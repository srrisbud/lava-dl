# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier:  BSD-3-Clause

import json
import os
from typing import Any, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import torch
from PIL import Image
from PIL.Image import Transpose
from torch.utils.data import Dataset
from torchvision import transforms

from ..boundingbox import utils as bbutils
from ..boundingbox.utils import Height, Width

"""BDD100K object detection dataset module."""


def removesuffix(input_string: str, suffix: str) -> str:
    """Removes suffix string from input string.

    Parameters
    ----------
    input_string : str
        Main input string.
    suffix : str
        Suffix to be removed.

    Returns
    -------
    str
        String without the suffix.
    """
    if suffix and input_string.endswith(suffix):
        return input_string[:-len(suffix)]
    return input_string


class _BDD(Dataset):
    # Low level BDD100K dataset. To be wrapped around, do not use externally.
    def __init__(self,
                 root: str = '.',
                 dataset: str = '.',
                 train: bool = False,
                 seq_len: int = 32,
                 randomize_seq: bool = False) -> None:
        super().__init__()
        self.seq_len = seq_len
        self.randomize_seq = randomize_seq

        image_set = 'train' if train else 'val'
        self.label_path = root + os.sep + \
            f'labels{os.sep}box_{dataset}_20{os.sep}{image_set}{os.sep}'
        if not os.path.isdir(self.label_path):
            msg = f'Could not find the label files in {self.label_path}. '
            msg += 'Download "MOT 2020 labels" from '
            msg += 'https://bdd-data.berkeley.edu/portal.html#download'
            raise FileNotFoundError(msg)
        self.image_path = root + os.sep + \
            f'images{os.sep}{dataset}{os.sep}{image_set}{os.sep}'
        if not os.path.isdir(self.image_path):
            msg = f'Could not find the image files in {self.image_path}.'
            msg += 'Download "MOT 2020 Images" from '
            msg += 'https://bdd-data.berkeley.edu/portal.html#download'
            raise FileNotFoundError(msg)
        json_files = [label_json for label_json in os.listdir(
            self.label_path) if label_json.endswith('.json')]

        categories = set()
        for json_file in json_files:
            with open(self.label_path + os.sep + json_file) as file:
                data = json.load(file)
                for img in data:
                    for cat in img['labels']:
                        categories.add(cat['category'])

        self.ids = [removesuffix(name, '.json') for name in json_files]
        self.cat_name = sorted(list(categories))
        self.idx_map = {name: idx for idx, name in enumerate(self.cat_name)}

    def _get_frame(self, path, labels):
        image = Image.open(path).convert('RGB')
        width, height = image.size
        size = {'height': height, 'width': width}
        objects = []
        for ann in labels:
            name = ann['category']
            bndbox = {'xmin': ann['box2d']['x1'],
                      'ymin': ann['box2d']['y1'],
                      'xmax': ann['box2d']['x2'],
                      'ymax': ann['box2d']['y2']}
            objects.append({'id': self.idx_map[name],
                            'name': name,
                            'bndbox': bndbox})

        annotation = {'annotation': {'size': size, 'object': objects}}
        return image, annotation

    def __getitem__(self, index: int) -> Tuple[torch.tensor, Dict[Any, Any]]:
        id = self.ids[index]
        img_path = self.image_path + os.sep + id + os.sep

        images = []
        annotations = []
        with open(self.label_path + os.sep + id + '.json') as file:
            data = json.load(file)
            num_seq = len(data)
            if self.randomize_seq:
                start_idx = np.random.randint(max(num_seq - self.seq_len, 0))
            else:
                start_idx = 0
            stop_idx = start_idx + self.seq_len
            data = data[start_idx:stop_idx]

            with ThreadPoolExecutor() as pool:
                path = map(lambda img: img_path + os.sep + img['name'], data)
                labels = map(lambda img: img['labels'], data)
                for image, annotation in pool.map(self._get_frame,
                                                  path, labels):
                    images.append(image)
                    annotations.append(annotation)
        if len(images) != self.seq_len:
            delta = self.seq_len - len(images)
            images = images + [images[-1]] * delta
            annotations = annotations + [annotations[-1]] * delta

        return images, annotations

    def __len__(self) -> int:
        return len(self.ids)


class BDD(Dataset):
    def __init__(self,
                 root: str = './',
                 dataset: str = 'track',
                 size: Tuple[Height, Width] = (448, 448),
                 train: bool = False,
                 seq_len: int = 32,
                 randomize_seq: bool = False,
                 augment_prob: float = 0.0) -> None:
        """Berkley Deep Drive (BDD100K) dataset module. For details on the
        dataset, refer to: https://bdd-data.berkeley.edu/.

        Parameters
        ----------
        root : str, optional
            Root folder where the dataset has been downloaded, by default './'
        dataset : str, optional
            Sub class of BDD100K dataset. By default 'track' which refers to
            MOT2020.
        size : Tuple[Height, Width], optional
            Desired spatial dimension of the frame, by default (448, 448)
        train : bool, optional
            Use training set. If false, testing set is used. By default False.
        seq_len : int, optional
            Number of sequential frames to process at a time, by default 32
        randomize_seq : bool, optional
            Randomize the start of frame sequence. If false, the first seq_len
            of the sample is returned, by default False.
        augment_prob : float, optional
            Augmentation probability of the frames and bounding boxes,
            by default 0.0.
        """
        super().__init__()
        self.blur = transforms.GaussianBlur(kernel_size=5)
        self.color_jitter = transforms.ColorJitter()
        self.grayscale = transforms.Grayscale(num_output_channels=3)
        self.img_transform = transforms.Compose([transforms.Resize(size),
                                                 transforms.ToTensor()])
        self.bb_transform = transforms.Compose([
            lambda x: bbutils.resize_bounding_boxes(x, size),
        ])

        self.datasets = [_BDD(root=root, dataset=dataset, train=train,
                              seq_len=seq_len, randomize_seq=randomize_seq)]

        self.classes = self.datasets[0].cat_name
        self.idx_map = self.datasets[0].idx_map
        self.augment_prob = augment_prob
        self.seq_len = seq_len
        self.randomize_seq = randomize_seq

    def flip_lr(self, img) -> Image:
        return Image.Image.transpose(img, Transpose.FLIP_LEFT_RIGHT)

    def __getitem__(self, index: int) -> Tuple[torch.tensor, Dict[Any, Any]]:
        """Get a sample video sequence of BDD100K dataset.

        Parameters
        ----------
        index : int
            Sample index.

        Returns
        -------
        Tuple[torch.tensor, Dict[Any, Any]]
            Frame sequence and dictionary of bounding box annotations.
        """
        dataset_idx = index // len(self.datasets[0])
        index = index % len(self.datasets[0])
        images, annotations = self.datasets[dataset_idx][index]

        # flip left right
        if np.random.random() < self.augment_prob:
            with ThreadPoolExecutor() as pool:
                images = pool.map(self.flip_lr, images)
            with ThreadPoolExecutor() as pool:
                annotations = pool.map(bbutils.fliplr_bounding_boxes,
                                       annotations)
        # blur
        if np.random.random() < self.augment_prob:
            with ThreadPoolExecutor() as pool:
                images = pool.map(self.blur, images)
        # color jitter
        if np.random.random() < self.augment_prob:
            with ThreadPoolExecutor() as pool:
                images = pool.map(self.color_jitter, images)
        # grayscale
        if np.random.random() < self.augment_prob:
            with ThreadPoolExecutor() as pool:
                images = pool.map(self.grayscale, images)

        with ThreadPoolExecutor() as pool:
            results = pool.map(self.img_transform, images)
            image = torch.stack(list(results), dim=-1)
        annotations = list(map(self.bb_transform, annotations))

        return image, annotations

    def __len__(self) -> int:
        """Number of samples in the dataset.
        """
        return sum([len(dataset) for dataset in self.datasets])
