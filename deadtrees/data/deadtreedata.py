import io
import logging
import math
from collections.abc import Iterable
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import albumentations as A
import webdataset as wds
from albumentations.pytorch import ToTensorV2

import numpy as np
import omegaconf
import PIL
import pytorch_lightning as pl
import torch
from deadtrees.loss.losses import class2one_hot, one_hot2dist
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning.trainer.supporters import CombinedLoader
from torch.utils.data import DataLoader

logger = logging.getLogger(__name__)


# NOTE: mean ans std computed on train shards
class DeadtreeDatasetConfig:
    """Dataset configuration"""

    # stats based on years 2017, 2018, 2019, 2020 and subtiles subsampled for 0.1
    mean = np.array([0.3661029729, 0.3875165941, 0.3501133538, 0.5797285859])
    std = np.array([0.2388708549, 0.2103625723, 0.2050272174, 0.2025812523])
    tile_size = 256
    fractions = [0.7, 0.2, 0.1]


class DeadtreeDatasetConfigImagenet:
    """Dataset configuration (imagenet pretrained encoder)"""

    # NIR channel info same as red
    mean = np.array([0.485, 0.456, 0.406, 0.485])
    std = np.array([0.229, 0.224, 0.225, 0.229])
    tile_size = 256
    fractions = [0.7, 0.2, 0.1]


def split_shards(original_list, split_fractions):
    """Distribute shards into train/ valid/ test sets according to provided split ratios"""

    assert np.isclose(
        sum(split_fractions), 1.0
    ), f"Split fractions do not sum to 1: {sum(split_fractions)}"

    original_list = [str(x) for x in sorted(original_list)]

    sublists = []
    prev_index = 0
    for weight in split_fractions:
        next_index = prev_index + int(round((len(original_list) * weight), 0))
        sublists.append(original_list[prev_index:next_index])
        prev_index = next_index

    assert sum([len(x) for x in sublists]) == len(original_list), "Split size mismatch"

    if not all(len(x) > 0 for x in sublists):
        logger.warning("Unexpected shard distribution encountered - trying to fix this")
        if len(split_fractions) == 3:
            if len(sublists[0]) > 2:
                sublists[0] = original_list[:-2]
                sublists[1] = original_list[-2:-1]
                sublists[2] = original_list[-1:]
            else:
                raise ValueError(
                    f"Not enough shards (#{len(original_list)}) for new distribution"
                )

        elif len(split_fractions) == 2:
            sublists[0] = original_list[:-1]
            sublists[1] = original_list[-1:]
        else:
            raise ValueError
        logger.warning(f"New shard split: {sublists}")

    if len(sublists) != 3:
        logger.warning("No test shards specified")
        sublists.append(None)

    return sublists


def image_decoder(data):
    with io.BytesIO(data) as stream:
        img = PIL.Image.open(stream)
        img.load()
        img = img.convert("RGBA")
    return np.asarray(img)


def mask_decoder(data):
    with io.BytesIO(data) as stream:
        img = PIL.Image.open(stream)
        img.load()
        img = img.convert("L")
    return np.asarray(img)


def sample_decoder(
    sample, img_suffix="rgbn.tif", msk_suffix="mask.tif", lu_suffix="lu.tif"
):
    """Decode data (image, mask, lu, stats) from sharded datastore"""

    assert img_suffix in sample, "Wrong image suffix provided"

    sample[img_suffix] = image_decoder(sample[img_suffix])

    if "txt" in sample:
        sample["txt"] = {"file": sample["__key__"], "frac": float(sample["txt"])}

    if msk_suffix in sample:
        sample[msk_suffix] = mask_decoder(sample[msk_suffix])

    if lu_suffix in sample:
        sample[lu_suffix] = mask_decoder(sample[lu_suffix])

    return sample


def inv_normalize(x):
    return lambda x: x * DeadtreeDatasetConfig.std + DeadtreeDatasetConfig.mean


train_transform = A.Compose(
    [
        # A.Resize(256,256),
        A.OneOf([A.HorizontalFlip(), A.VerticalFlip()], p=0.5),
        A.RandomRotate90(p=0.5),
        A.RandomBrightnessContrast(
            p=0.5,
            brightness_limit=0.2,
            contrast_limit=0.15,
            brightness_by_max=False,
        ),
        A.Normalize(mean=DeadtreeDatasetConfig.mean, std=DeadtreeDatasetConfig.std),
        ToTensorV2(),
    ]
)

val_transform = A.Compose(
    [
        # A.Resize(256,256),
        A.Normalize(mean=DeadtreeDatasetConfig.mean, std=DeadtreeDatasetConfig.std),
        ToTensorV2(),
    ]
)


def transform(
    sample: dict,
    *,
    transform_func: Optional[Callable] = None,
    in_channels: Optional[int] = 4,
    classes: Optional[int] = 3,
    distmap: Optional[bool] = False,
):
    """Apply transform func to sample and modify channels and/ or classes as specified in training setup"""
    if transform_func:
        transformed = transform_func(
            image=sample["image"].copy(),
            mask=sample["mask"].copy(),
            lu=sample["lu"].copy(),
        )
        sample["image"] = transformed["image"].float()
        sample["mask"] = transformed["mask"].long()
        sample["lu"] = transformed["lu"]

    sample["image"] = sample["image"][0:in_channels]
    sample["lu"] = torch.tensor(sample["lu"], dtype=torch.long)

    if classes == 2:
        sample["mask"][sample["mask"] > 1] = 1

    if distmap:
        x = class2one_hot(torch.unsqueeze(sample["mask"], dim=0), K=classes)[0]
        x = one_hot2dist(x.cpu().numpy(), resolution=[1, 1])
        sample["distmap"] = torch.tensor(x, dtype=torch.float32)
    else:
        sample["distmap"] = None

    return sample


class DeadtreesDataModule(pl.LightningDataModule):
    def __init__(
        self,
        data_dir: Union[List, str],
        pattern: str,
        pattern_extra: Optional[List[str]] = None,
        batch_size_extra: Optional[List[int]] = None,
        train_dataloader_conf: Optional[DictConfig] = None,
        val_dataloader_conf: Optional[DictConfig] = None,
        test_dataloader_conf: Optional[DictConfig] = None,
    ):
        super().__init__()

        print(type(data_dir))
        print(data_dir)
        if isinstance(data_dir, Iterable):
            self.data_shards = [sorted(Path(d).glob(pattern)) for d in data_dir]
            self.layout = "train/val/test"
        else:
            self.data_shards = sorted(Path(data_dir).glob(pattern))
            self.layout = "single_directory"

        self.train_dataloader_conf = train_dataloader_conf or OmegaConf.create()
        self.val_dataloader_conf = val_dataloader_conf or OmegaConf.create()
        self.test_dataloader_conf = test_dataloader_conf or OmegaConf.create()

        self.data_shards_extra = []
        self.batch_size_extra = []

        if pattern_extra and batch_size_extra:
            if self.layout == "train/val/test":
                raise ValueError(
                    "Combining pattern_extra with train/val/test layout not allowed"
                )

            for pcnt, p in enumerate(pattern_extra):
                self.data_shards_extra.append(sorted(Path(data_dir).glob(p)))
            if len(batch_size_extra) > 0:
                if len(batch_size_extra) != len(pattern_extra):
                    raise ValueError(
                        "Len of <pattern_extra> and <batch_size_extra> don't match"
                    )
                self.batch_size_extra = batch_size_extra
            else:
                raise ValueError(
                    "<pattern_extra> provided but no <batch_size_extra> ratio found"
                )

    def setup(
        self,
        stage=None,  # required for 1.6
        split_fractions: List[float] = DeadtreeDatasetConfig.fractions,
        in_channels: Optional[int] = 4,  # change to 3 for rgb training instead of rgbn
        classes: Optional[int] = 3,  # change to 2 for single class (+bg) setup
    ) -> None:
        if self.layout == "single_directory":
            train_shards, valid_shards, test_shards = split_shards(
                self.data_shards, split_fractions
            )
        else:
            train_shards, valid_shards, test_shards = self.data_shards
            train_shards = [str(x) for x in train_shards]
            valid_shards = [str(x) for x in valid_shards]
            test_shards = [str(x) for x in test_shards]

        # determine the length of the dataset
        shard_size = sum(1 for _ in DataLoader(wds.WebDataset(train_shards[0])))
        logger.info(
            f"Shard size: {shard_size} (estimate base on file: {train_shards[0]})"
        )

        def build_dataset(
            shards: List[str],
            bs: int,
            transform_func: Callable,
            shuffle: Optional[int] = 128,
            shard_size: Optional[int] = 128,
        ) -> wds.WebDataset:
            return (
                wds.WebDataset(
                    shards,
                    length=len(shards) * shard_size // bs,
                )
                .shuffle(shuffle)
                .map(sample_decoder)
                .rename(image="rgbn.tif", mask="mask.tif", lu="lu.tif", stats="txt")
                .map(
                    partial(
                        transform,
                        transform_func=transform_func,
                        in_channels=in_channels,
                        classes=classes,
                        distmap=True,
                    )
                )
                .to_tuple("image", "mask", "distmap", "lu", "stats")
            )

        self.train_data = build_dataset(
            train_shards,
            self.train_dataloader_conf["batch_size"],
            transform_func=train_transform,
            shuffle=shard_size,
            shard_size=shard_size,
        )

        self.val_data = build_dataset(
            valid_shards,
            self.val_dataloader_conf["batch_size"],
            transform_func=val_transform,
            shuffle=0,
            shard_size=shard_size,
        )

        if test_shards:
            self.test_data = build_dataset(
                test_shards,
                self.test_dataloader_conf["batch_size"],
                transform_func=val_transform,
                shuffle=0,
                shard_size=shard_size,
            )

        self.extra_train_data = []
        self.extra_valid_data = []

        if len(self.data_shards_extra) > 0:
            for bs, shards in zip(self.batch_size_extra, self.data_shards_extra):
                # split shards between train and val by the same proportion as the main dataset
                train_frac = len(train_shards) / (len(train_shards) + len(valid_shards))
                valid_frac = 1 - train_frac

                extra_train_shards, extra_valid_shards, _ = split_shards(
                    shards, [train_frac, valid_frac]
                )

                self.extra_train_data.append(
                    build_dataset(
                        extra_train_shards,
                        bs,
                        transform_func=train_transform,
                        shuffle=shard_size,
                        shard_size=shard_size,
                    )
                )

                self.extra_valid_data.append(
                    build_dataset(
                        extra_valid_shards,
                        bs,
                        transform_func=val_transform,
                        shuffle=0,
                        shard_size=shard_size,
                    )
                )

    def train_dataloader(self) -> Dict[str, DataLoader]:
        main_loader = DataLoader(
            self.train_data.batched(
                self.train_dataloader_conf["batch_size"] - sum(self.batch_size_extra),
                partial=False,
            ),
            batch_size=None,
            pin_memory=False,
            num_workers=self.train_dataloader_conf["num_workers"],
        )

        loaders = {"main": main_loader}
        for cnt, (bs, train_data) in enumerate(
            zip(self.batch_size_extra, self.extra_train_data)
        ):
            loaders[f"extra_{cnt}"] = DataLoader(
                train_data.batched(bs, partial=False),
                batch_size=None,
                pin_memory=True,
                num_workers=bs // 2,
            )

        return loaders

    def val_dataloader(self) -> List[DataLoader]:
        main_loader = DataLoader(
            self.val_data.batched(
                self.val_dataloader_conf["batch_size"] - sum(self.batch_size_extra),
                partial=False,
            ),
            batch_size=None,
            pin_memory=False,
            num_workers=self.val_dataloader_conf["num_workers"],
        )

        loaders = {"main": main_loader}
        for cnt, (bs, val_data) in enumerate(
            zip(self.batch_size_extra, self.extra_valid_data)
        ):
            loaders[f"extra_{cnt}"] = DataLoader(
                val_data.batched(bs, partial=False),
                batch_size=None,
                pin_memory=True,
                num_workers=bs // 2,
            )

        combined_loaders = CombinedLoader(loaders, "max_size_cycle")
        return combined_loaders

    def test_dataloader(self) -> DataLoader:
        return DataLoader(
            self.test_data.batched(
                self.test_dataloader_conf["batch_size"], partial=False
            ),
            batch_size=None,
            pin_memory=False,
            num_workers=self.test_dataloader_conf["num_workers"],
        )
