# Copyright (c) 2021 - present / Neuralmagic, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import re
import struct
from typing import Dict, Iterable, Optional, Tuple, Union

from safetensors import safe_open
from torch import Tensor
from transformers.utils import SAFE_WEIGHTS_INDEX_NAME, SAFE_WEIGHTS_NAME, cached_file


__all__ = [
    "get_safetensors_folder",
    "get_safetensors_header",
    "match_param_name",
    "merge_names",
    "get_weight_mappings",
    "get_nested_weight_mappings",
    "get_nested_mappings_from_state_dict",
    "get_quantization_parameter_to_path_mapping",
    "is_quantization_param",
]

NestedStateDictType = Dict[str, Dict[str, Tensor]]
WeightMappingType = Dict[str, str]
NestedWeightMappingType = Dict[str, WeightMappingType]


def get_safetensors_folder(
    pretrained_model_name_or_path: str, cache_dir: Optional[str] = None
) -> str:
    """
    Given a Hugging Face stub or a local path, return the folder containing the
    safetensors weight files

    :param pretrained_model_name_or_path: local path to model or HF stub
    :param cache_dir: optional cache dir to search through, if none is specified the
    model will be searched for in the default TRANSFORMERS_CACHE
    :return: local folder containing model data
    """
    if os.path.exists(pretrained_model_name_or_path):
        # argument is a path to a local folder
        return os.path.abspath(pretrained_model_name_or_path)

    safetensors_path = cached_file(
        pretrained_model_name_or_path,
        SAFE_WEIGHTS_NAME,
        cache_dir=cache_dir,
        _raise_exceptions_for_missing_entries=False,
    )
    index_path = cached_file(
        pretrained_model_name_or_path,
        SAFE_WEIGHTS_INDEX_NAME,
        cache_dir=cache_dir,
        _raise_exceptions_for_missing_entries=False,
    )
    if safetensors_path is not None:
        # found a single cached safetensors file
        return os.path.split(safetensors_path)[0]
    if index_path is not None:
        # found a cached safetensors weight index file
        return os.path.split(index_path)[0]

    # model weights could not be found locally or cached from HF Hub
    raise ValueError(
        "Could not locate safetensors weight or index file from "
        f"{pretrained_model_name_or_path}."
    )


def get_safetensors_header(safetensors_path: str) -> Dict[str, str]:
    """
    Extracts the metadata from a safetensors file as JSON

    :param safetensors_path: path to a safetensors file
    :return: dictionary of metadata extracted from the safetensors file
    """
    with open(safetensors_path, "rb") as f:
        length_of_header = struct.unpack("<Q", f.read(8))[0]
        header_data = f.read(length_of_header)
        header = json.loads(header_data)

    return header


def match_param_name(full_name: str, param_name: str) -> Optional[str]:
    """
    Helper function extracting the uncompressed parameterized layer name from a
    compressed name. Assumes the compressed name was merged using merge_names.

    :param full_name: full name of parameter in compressed model
    :param param_name: compression paramater name
    :return: uncompressed name of the uncompressed parameterized layer
    """
    pattern = r"^(.*)\." + param_name + r"$"
    regex = re.findall(pattern, full_name)
    if len(regex) == 0:
        return None
    return regex[0]


def merge_names(parent_name: str, child_name: str) -> str:
    """
    Helper function for merging an uncompressed parameterized layer name with a
    compression parameter. Names merged with this function can then be parsed by
    match_param_name.

    :param parent_name: uncompressed parameterized layer name
    :param child_name: compression parameter name
    :return: merged compressed name
    """
    return parent_name + "." + child_name


def get_weight_mappings(path_to_model_or_tensors: str) -> Dict[str, str]:
    """
    Takes a path to a state dict saved in safetensors format and returns a mapping
    from parameterized layer name to file location.

    {
        layer.weight.bitmask: file_location,
        layer.weight.row_offsets: file_location,
        layer.weight.shape: file_location,
        layer.weight.compressed: file_location
    }

    This generalizes to cases where the model is split into multiple safetensors files

    :param path_to_model_or_tensors: path to directory that contains
        safetensors (must contain either a single file or multiple files with an index),
        or a path to a single safetensors file
    :return: mapping of parameterized layer name to file location
    """

    if os.path.isfile(path_to_model_or_tensors):
        # we have a single safetensors file to read
        header = get_safetensors_header(path_to_model_or_tensors)
        for key in header.keys():
            header[key] = path_to_model_or_tensors
        header.pop("__metadata__", None)
    else:
        # we have a directory with multiple safetensors files
        safetensors_path = os.path.join(path_to_model_or_tensors, SAFE_WEIGHTS_NAME)
        index_path = os.path.join(path_to_model_or_tensors, SAFE_WEIGHTS_INDEX_NAME)
        if os.path.exists(safetensors_path):
            # we have a single safetensors file to read
            header = get_safetensors_header(safetensors_path)
            for key in header.keys():
                header[key] = SAFE_WEIGHTS_NAME
            header.pop("__metadata__", None)
        elif os.path.exists(index_path):
            # we have multiple safetensors file, read from index
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            header = index["weight_map"]
        else:
            raise ValueError(
                "Could not find a safetensors weight "
                f"or index file at {path_to_model_or_tensors}"
            )

        # convert weight locations to full paths
        for key, value in header.items():
            header[key] = os.path.join(path_to_model_or_tensors, value)

    return header


def get_nested_weight_mappings(
    model_path: str,
    params_to_nest: Iterable[str],
    return_unmatched_params: bool = False,
) -> Union[NestedWeightMappingType, Tuple[NestedWeightMappingType, WeightMappingType]]:
    """
    Takes a path to a state dict saved in safetensors format and returns a nested
    mapping from uncompressed parameterized layer names to the file locations of
    each layer's compression parameters.

    Example of the nested mapping:
    layer: {
        bitmask: file_location,
        row_offsets: file_location,
        shape: file_location,
        compressed: file_location
    }

    If other parameters are found that do not match the nested parameters, they will
    be returned in a separate dictionary only if return_unmatched_params is True.
    This dictionary may be needed for cases where compressors are stacked (e.g.,
    quantization compression followed by sparse compression).

    Example of the unmatched params mapping:
    {
        layer.weight_scale: file_location,
        layer.input_scale: file_location
    }

    This generalizes to cases where the model is split into multiple safetensors
    files.

    :param model_path: Path to the safetensors state dict, must contain either a
        single safetensors file or multiple files with an index.
    :param params_to_nest: Iterable of parameter names to nest.
    :param return_unmatched_params: If True, return a second dictionary containing
        the remaining parameters that were not matched to the params_to_nest.
    :return:
        - If return_unmatched_params is False:
            NestedWeightMappingType: A nested mapping of parameterized layer names to
            file locations of each layer's compression parameters.
        - If return_unmatched_params is True:
            Tuple[NestedWeightMappingType, WeightMappingType]: A tuple containing:
                - NestedWeightMappingType: A nested mapping of parameterized layer
                names to file locations of each layer's compression parameters.
                - WeightMappingType: A mapping of the remaining parameter names to
                their file locations that were not matched to the params_to_nest.
    """
    weight_mappings = get_weight_mappings(model_path)
    nested_weight_mappings = {}
    unmatched_params = {}

    for key, file_location in weight_mappings.items():
        matched = False
        for param_name in params_to_nest:
            module_path = match_param_name(key, param_name)
            if module_path:
                if module_path not in nested_weight_mappings:
                    nested_weight_mappings[module_path] = {}
                nested_weight_mappings[module_path][param_name] = file_location
                matched = True
        if return_unmatched_params and not matched:
            unmatched_params[key] = file_location

    if return_unmatched_params:
        return nested_weight_mappings, unmatched_params
    return nested_weight_mappings


def get_nested_mappings_from_state_dict(
    state_dict: Dict[str, Tensor],
    params_to_nest: Iterable[str],
    return_unmatched_params: bool = False,
) -> Union[NestedStateDictType, Tuple[NestedStateDictType, Dict[str, Tensor]]]:
    """
    Takes a state dict and returns a nested mapping from uncompressed
    parameterized layer names to the value of
    each layer's compression parameters.

    Example of the nested mapping:
    layer: {
        weight_scale: ...,
        weight: ...,
        zero_point: ...,
    }

    :param state_dict: state dict of the model
    :param params_to_nest: Iterable of parameter names to nest.
    :return: Nested mapping of parameterized layer names to the value of
        each layer's compression parameters. If `return_unmatched_params`, then
        also return a dictionary mapping unused parameter names to their values
    """
    nested_weight_mappings = {}
    unmatched_params = {}

    for key in state_dict.keys():
        matched = False
        for param_name in params_to_nest:
            module_path = match_param_name(key, param_name)
            if module_path:
                if module_path not in nested_weight_mappings:
                    nested_weight_mappings[module_path] = {}
                nested_weight_mappings[module_path][param_name] = state_dict[key]
                matched = True
        if return_unmatched_params and not matched:
            unmatched_params[key] = state_dict[key]

    if return_unmatched_params:
        return nested_weight_mappings, unmatched_params
    return nested_weight_mappings


def get_quantization_parameter_to_path_mapping(model_path: str) -> Dict[str, str]:
    """
    Given a model path, return a mapping between a parameter and its path
    on disk
    """
    weight_mappings = get_weight_mappings(model_path)
    mapping = {}
    for weight_name, safe_path in weight_mappings.items():
        if is_quantization_param(weight_name):
            mapping[weight_name] = safe_path
            continue
    return mapping


def is_quantization_param(name: str) -> bool:
    """
    Checks is a parameter name is associated with a quantization parameter

    :param name: parameter name to check
    :return: True if parameter name is a quantization parameter, else False
    """
    if name.endswith("_scale"):
        return True
    if name.endswith("zero_point"):
        return True
    if name.endswith("g_idx"):
        return True

    return False
