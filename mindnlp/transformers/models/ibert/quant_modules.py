# coding=utf-8
# Copyright 2021 The I-BERT Authors (Sehoon Kim, Amir Gholami, Zhewei Yao,
# Michael Mahoney, Kurt Keutzer - UC Berkeley) and The HuggingFace Inc. team.
# Copyright (c) 20121, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# pylint: disable=access-member-before-definition
"""quant modules"""
import decimal

import numpy as np
import mindspore
from mindspore.nn import Cell
from mindnlp.core import nn, ops, no_grad

from ....utils import logging


logger = logging.get_logger(__name__)


class QuantEmbedding(nn.Module):
    """
    Quantized version of `nn.Embedding`. Adds quantization-specific arguments on top of `nn.Embedding`.

    Args:
        weight_bit (`int`, *optional*, defaults to `8`):
            Bitwidth for the quantized weight.
        momentum (`float`, *optional*, defaults to `0.95`):
            Momentum for updating the activation quantization range.
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
    """

    def __init__(
        self,
        num_embeddings,
        embedding_dim,
        padding_idx=None,
        max_norm=None,
        norm_type=2.0,
        scale_grad_by_freq=False,
        sparse=False,
        _weight=None,
        weight_bit=8,
        momentum=0.95,
        quant_mode=False,
    ):
        super().__init__()
        self.num_ = num_embeddings
        self.dim = embedding_dim
        self.padding_idx = padding_idx
        self.max_norm = max_norm
        self.norm_type = norm_type
        self.scale_grad_by_freq = scale_grad_by_freq
        self.sparse = sparse

        self.weight = nn.Parameter(ops.zeros(num_embeddings, embedding_dim))
        self.register_buffer("weight_scaling_factor", ops.zeros(1))
        self.register_buffer("weight_integer", ops.zeros_like(self.weight))

        self.weight_bit = weight_bit
        self.momentum = momentum
        self.quant_mode = quant_mode
        self.percentile_mode = False

    def forward(self, x, positions=None, incremental_state=None):
        if not self.quant_mode:
            return (
                nn.functional.embedding(
                    x,
                    self.weight,
                    self.padding_idx,
                    self.max_norm,
                    self.norm_type,
                    self.scale_grad_by_freq,
                ),
                None,
            )

        w = self.weight
        w_transform = w.data
        w_min = w_transform.min().broadcast_to((1,))
        w_max = w_transform.max().broadcast_to((1,))

        self.weight_scaling_factor = symmetric_linear_quantization_params(self.weight_bit, w_min, w_max, False)
        self.weight_integer = SymmetricQuantFunction(self.weight_bit, self.percentile_mode, self.weight_scaling_factor)(self.weight)

        emb_int = nn.functional.embedding(
            x,
            self.weight_integer,
            self.padding_idx,
            self.max_norm,
            self.norm_type,
            self.scale_grad_by_freq,
        )
        return emb_int * self.weight_scaling_factor, self.weight_scaling_factor


class QuantAct(nn.Module):
    """
    Quantizes the given activation.

    Args:
        activation_bit (`int`):
            Bitwidth for the quantized activation.
        act_range_momentum (`float`, *optional*, defaults to `0.95`):
            Momentum for updating the activation quantization range.
        per_channel (`bool`, *optional*, defaults to `False`):
            Whether to or not use channel-wise quantization.
        channel_len (`int`, *optional*):
            Specify the channel length when set the *per_channel* True.
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
    """

    def __init__(self, activation_bit, act_range_momentum=0.95, per_channel=False, channel_len=None, quant_mode=False):
        super().__init__()

        self.activation_bit = activation_bit
        self.act_range_momentum = act_range_momentum
        self.quant_mode = quant_mode
        self.per_channel = per_channel
        self.percentile = False

        if not self.per_channel:
            self.register_buffer("x_min", ops.zeros(1) - 1e-5)
            self.register_buffer("x_max", ops.zeros(1) + 1e-5)
            self.register_buffer("act_scaling_factor", ops.zeros(1))
            # self.x_min -= 1e-5
            # self.x_max += 1e-5
        else:
            raise NotImplementedError("per-channel mode is not currently supported for activation.")

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(activation_bit={self.activation_bit}, "
            f"quant_mode: {self.quant_mode}, Act_min: {self.x_min.item():.2f}, "
            f"Act_max: {self.x_max.item():.2f})"
        )

    def forward(
        self,
        x,
        pre_act_scaling_factor=None,
        identity=None,
        identity_scaling_factor=None,
        specified_min=None,
        specified_max=None,
    ):
        x_act = x if identity is None else identity + x
        # collect running stats if training
        if self.training:
            assert not self.percentile, "percentile mode is not currently supported for activation."
            assert not self.per_channel, "per-channel mode is not currently supported for activation."
            x_min = x_act.min()
            x_max = x_act.max()

            assert (
                x_max.isnan().sum() == 0 and x_min.isnan().sum() == 0
            ), "NaN detected when computing min/max of the activation"

            # Initialization
            if self.x_min.min() > -1.1e-5 and self.x_max.max() < 1.1e-5:
                self.x_min = self.x_min + x_min
                self.x_max = self.x_max + x_max

            # exponential moving average (EMA)
            # use momentum to prevent the quantized values change greatly every iteration
            elif self.act_range_momentum == -1:
                self.x_min = ops.min(self.x_min, x_min)
                self.x_max = ops.max(self.x_max, x_max)
            else:
                self.x_min = self.x_min * self.act_range_momentum + x_min * (1 - self.act_range_momentum)
                self.x_max = self.x_max * self.act_range_momentum + x_max * (1 - self.act_range_momentum)

        if not self.quant_mode:
            return x_act, None

        x_min = self.x_min if specified_min is None else specified_min
        x_max = self.x_max if specified_max is None else specified_max

        self.act_scaling_factor = symmetric_linear_quantization_params(
            self.activation_bit, x_min, x_max, per_channel=self.per_channel
        )

        if pre_act_scaling_factor is None:
            # this is for the input quantization
            quant_act_int = SymmetricQuantFunction(self.activation_bit, self.percentile, self.act_scaling_factor)(x)
        else:
            quant_act_int = FixedPointMul(pre_act_scaling_factor, self.activation_bit, self.act_scaling_factor, identity_scaling_factor)(x, identity)


        correct_output_scale = self.act_scaling_factor.view(-1)

        return quant_act_int * correct_output_scale, self.act_scaling_factor


class QuantLinear(nn.Module):
    """
    Quantized version of `nn.Linear`. Adds quantization-specific arguments on top of `nn.Linear`.

    Args:
        weight_bit (`int`, *optional*, defaults to `8`):
            Bitwidth for the quantized weight.
        bias_bit (`int`, *optional*, defaults to `32`):
            Bitwidth for the quantized bias.
        per_channel (`bool`, *optional*, defaults to `False`):
            Whether or not to use channel-wise quantization.
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
    """

    def __init__(
        self, in_features, out_features, bias=True, weight_bit=8, bias_bit=32, per_channel=False, quant_mode=False
    ):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features

        self.weight = nn.Parameter(ops.zeros(out_features, in_features))
        self.register_buffer("weight_integer", ops.zeros_like(self.weight))
        self.register_buffer("fc_scaling_factor", ops.zeros(self.out_features))
        if bias:
            self.bias = nn.Parameter(ops.zeros(out_features))
            self.register_buffer("bias_integer", ops.zeros_like(self.bias))

        self.weight_bit = weight_bit
        self.quant_mode = quant_mode
        self.per_channel = per_channel
        self.bias_bit = bias_bit
        self.quant_mode = quant_mode
        self.percentile_mode = False

    def __repr__(self):
        s = super().__repr__()
        s = f"({s} weight_bit={self.weight_bit}, quant_mode={self.quant_mode})"
        return s

    def forward(self, x, prev_act_scaling_factor=None):
        if not self.quant_mode:
            return nn.functional.linear(x, weight=self.weight, bias=self.bias), None

        # assert that prev_act_scaling_factor is a scalar tensor
        assert prev_act_scaling_factor is not None and prev_act_scaling_factor.shape == (1,), (
            "Input activation to the QuantLinear layer should be globally (non-channel-wise) quantized. "
            "Please add a QuantAct layer with `per_channel = True` before this QuantAct layer"
        )

        w = self.weight
        w_transform = w.data
        if self.per_channel:
            w_min, _ = ops.min(w_transform, dim=1)
            w_max, _ = ops.max(w_transform, dim=1)
        else:
            w_min = w_transform.min().broadcast_to((1,))
            w_max = w_transform.max().broadcast_to((1,))

        self.fc_scaling_factor = symmetric_linear_quantization_params(self.weight_bit, w_min, w_max, self.per_channel)
        self.weight_integer = SymmetricQuantFunction(self.weight_bit, self.percentile_mode, self.fc_scaling_factor)(self.weight)


        bias_scaling_factor = self.fc_scaling_factor * prev_act_scaling_factor

        if self.bias is not None:
            self.bias_integer = SymmetricQuantFunction(self.bias_bit, False, bias_scaling_factor)(self.bias)

        prev_act_scaling_factor = prev_act_scaling_factor.view(1, -1)
        x_int = x / prev_act_scaling_factor

        return (
            nn.functional.linear(x_int, weight=self.weight_integer, bias=self.bias_integer) * bias_scaling_factor,
            bias_scaling_factor,
        )


class IntGELU(nn.Module):
    """
    Quantized version of `nn.GELU`. Adds quantization-specific arguments on top of `nn.GELU`.

    Args:
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
        force_dequant (`str`, *optional*, defaults to `"none"`):
            Force dequantize the layer if either "gelu" or "nonlinear" is given.
    """

    def __init__(self, quant_mode=True, force_dequant="none"):
        super().__init__()
        self.quant_mode = quant_mode

        if force_dequant in ["nonlinear", "gelu"]:
            logger.info("Force dequantize gelu")
            self.quant_mode = False

        if not self.quant_mode:
            self.activation_fn = nn.GELU()

        self.k = 1.4142
        self.const = 14  # dummy integer constant
        self.coeff = [-0.2888, -1.769, 1]  # a(x+b)**2 + c
        self.coeff[2] /= self.coeff[0]

    def int_erf(self, x_int, scaling_factor):
        b_int = ops.floor(self.coeff[1] / scaling_factor)
        c_int = ops.floor(self.coeff[2] / scaling_factor**2)
        sign = ops.sign(x_int)

        abs_int = ops.minimum(ops.abs(x_int), -b_int)
        y_int = sign * ((abs_int + b_int) ** 2 + c_int)
        scaling_factor = scaling_factor**2 * self.coeff[0]

        # avoid overflow
        y_int = floor_ste()(y_int / 2**self.const)
        scaling_factor = scaling_factor * 2**self.const

        return y_int, scaling_factor

    def forward(self, x, scaling_factor=None):
        if not self.quant_mode:
            return self.activation_fn(x), None

        x_int = x / scaling_factor
        sigmoid_int, sigmoid_scaling_factor = self.int_erf(x_int, scaling_factor / self.k)

        shift_int = 1.0 // sigmoid_scaling_factor

        x_int = x_int * (sigmoid_int + shift_int)
        scaling_factor = scaling_factor * sigmoid_scaling_factor / 2

        return x_int * scaling_factor, scaling_factor


class IntSoftmax(nn.Module):
    """
    Quantized version of `nn.Softmax`. Adds quantization-specific arguments on top of `nn.Softmax`.

    Args:
        output_bit (`int`):
            Bitwidth for the layer output activation.
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
        force_dequant (`str`, *optional*, defaults to `"none"`):
            Force dequantize the layer if either "softmax" or "nonlinear" is given.
    """

    def __init__(self, output_bit, quant_mode=False, force_dequant="none"):
        super().__init__()
        self.output_bit = output_bit
        self.max_bit = 32
        self.quant_mode = quant_mode

        if force_dequant in ["nonlinear", "softmax"]:
            logger.info("Force dequantize softmax")
            self.quant_mode = False

        self.act = QuantAct(16, quant_mode=self.quant_mode)
        self.x0 = -0.6931  # -ln2
        self.const = 30  # dummy integer constant
        self.coef = [0.35815147, 0.96963238, 1.0]  # ax**2 + bx + c
        self.coef[1] /= self.coef[0]
        self.coef[2] /= self.coef[0]

    def int_polynomial(self, x_int, scaling_factor):
        with no_grad():
            b_int = ops.floor(self.coef[1] / scaling_factor)
            c_int = ops.floor(self.coef[2] / scaling_factor**2)
        z = (x_int + b_int) * x_int + c_int
        scaling_factor = self.coef[0] * scaling_factor**2
        return z, scaling_factor

    def int_exp(self, x_int, scaling_factor):
        with no_grad():
            x0_int = ops.floor(self.x0 / scaling_factor)
        x_int = ops.maximum(x_int, self.const * x0_int)

        q = floor_ste()(x_int / x0_int)
        r = x_int - x0_int * q
        exp_int, exp_scaling_factor = self.int_polynomial(r, scaling_factor)
        exp_int = ops.clamp(floor_ste()(exp_int * 2 ** (self.const - q)), min=0)
        scaling_factor = exp_scaling_factor / 2**self.const
        return exp_int, scaling_factor

    def forward(self, x, scaling_factor):
        if not self.quant_mode:
            return nn.functional.softmax(x, dim=-1), None

        x_int = x / scaling_factor

        x_int_max, _ = ops.max(x_int, dim=-1, keepdim=True)
        x_int = x_int - x_int_max
        exp_int, exp_scaling_factor = self.int_exp(x_int, scaling_factor)

        # Avoid overflow
        exp, exp_scaling_factor = self.act(exp_int, exp_scaling_factor)
        exp_int = exp / exp_scaling_factor

        exp_int_sum = ops.sum(exp_int, dim=-1, keepdim=True)
        factor = floor_ste()(2**self.max_bit / exp_int_sum)
        exp_int = floor_ste()(exp_int * factor / 2 ** (self.max_bit - self.output_bit))
        scaling_factor = 1 / 2**self.output_bit
        return exp_int * scaling_factor, scaling_factor


class IntLayerNorm(nn.Module):
    """
    Quantized version of `nn.LayerNorm`. Adds quantization-specific arguments on top of `nn.LayerNorm`.

    Args:
        output_bit (`int`, *optional*, defaults to `8`):
            Bitwidth for the layer output activation.
        quant_mode (`bool`, *optional*, defaults to `False`):
            Whether or not the layer is quantized.
        force_dequant (`str`, *optional*, defaults to `"none"`):
            Force dequantize the layer if either "layernorm" or "nonlinear" is given.
    """

    def __init__(self, normalized_shape, eps, output_bit=8, quant_mode=False, force_dequant="none"):
        super().__init__()
        self.normalized_shape = normalized_shape
        self.eps = eps

        self.weight = nn.Parameter(ops.zeros(normalized_shape))
        self.bias = nn.Parameter(ops.zeros(normalized_shape))

        self.quant_mode = quant_mode
        if force_dequant in ["nonlinear", "layernorm"]:
            logger.info("Force dequantize layernorm")
            self.quant_mode = False

        self.register_buffer("shift", ops.zeros(1))
        self.output_bit = output_bit
        self.max_bit = 32
        self.dim_sqrt = None
        self.activation = QuantAct(self.output_bit, quant_mode=self.quant_mode)

    def set_shift(self, y_int):
        with no_grad():
            y_sq_int = y_int**2
            var_int = ops.sum(y_sq_int, dim=2, keepdim=True)
            shift = (ops.log2(ops.sqrt(var_int / 2**self.max_bit)).ceil()).max()
            shift_old = self.shift
            self.shift = ops.maximum(self.shift, shift)
            logger.info(f"Dynamic shift adjustment: {int(shift_old)} -> {int(self.shift)}")

    def overflow_fallback(self, y_int):
        """
        This fallback function is called when overflow is detected during training time, and adjusts the `self.shift`
        to avoid overflow in the subsequent runs.
        """
        self.set_shift(y_int)  # adjusts `self.shift`
        y_int_shifted = floor_ste()(y_int / 2**self.shift)
        y_sq_int = y_int_shifted**2
        var_int = ops.sum(y_sq_int, dim=2, keepdim=True)
        return var_int

    def forward(self, x, scaling_factor=None):
        if not self.quant_mode:
            mean = ops.mean(x, dim=2, keepdim=True)
            y = x - mean
            var = ops.mean(y**2, dim=2, keepdim=True)
            x = y / ops.sqrt(self.eps + var)
            x = x * self.weight + self.bias
            return x, None

        # compute sqrt of the feature dimension if it is the first run
        if self.dim_sqrt is None:
            n = mindspore.tensor(x.shape[2], dtype=mindspore.float32)
            self.dim_sqrt = ops.sqrt(n)

        # Normalization: computes mean and variance(std)
        x_int = x / scaling_factor
        mean_int = round_ste()(x_int.mean(axis=2, keep_dims=True))
        y_int = x_int - mean_int
        y_int_shifted = floor_ste()(y_int / 2**self.shift)
        y_sq_int = y_int_shifted**2
        var_int = ops.sum(y_sq_int, dim=2, keepdim=True)

        # overflow handling in training time
        if self.training:
            # if overflow is detected
            if var_int.max() >= 2**self.max_bit:
                var_int = self.overflow_fallback(y_int)
                assert var_int.max() < 2**self.max_bit + 0.1, (
                    "Error detected in overflow handling: "
                    "`var_int` exceeds `self.max_bit` (the maximum possible bit width)"
                )

        # To be replaced with integer-sqrt kernel that produces the same output
        std_int = floor_ste()(ops.sqrt(var_int)) * 2**self.shift
        factor = floor_ste()(2**31 / std_int)
        y_int = floor_ste()(y_int * factor / 2)
        scaling_factor = self.dim_sqrt / 2**30

        # scaling and shifting
        bias = self.bias / (self.weight)
        bias_int = floor_ste()(bias / scaling_factor)

        y_int = y_int + bias_int
        scaling_factor = scaling_factor * self.weight
        x = y_int * scaling_factor

        return x, scaling_factor


def get_percentile_min_max(input, lower_percentile, upper_percentile, output_tensor=False):
    """
    Calculate the percentile max and min values in a given tensor

    Args:
        input (`mindspore.Tensor`):
            The target tensor to calculate percentile max and min.
        lower_percentile (`float`):
            If 0.1, means we return the value of the smallest 0.1% value in the tensor as percentile min.
        upper_percentile (`float`):
            If 99.9, means we return the value of the largest 0.1% value in the tensor as percentile max.
        output_tensor (`bool`, *optional*, defaults to `False`):
            If True, this function returns tensors, otherwise it returns values.

    Returns:
        `Tuple(mindspore.Tensor, mindspore.Tensor)`: Percentile min and max value of *input*
    """
    input_length = input.shape[0]

    lower_index = round(input_length * (1 - lower_percentile * 0.01))
    upper_index = round(input_length * upper_percentile * 0.01)

    upper_bound = ops.kthvalue(input, k=upper_index).values

    if lower_percentile == 0:
        lower_bound = upper_bound * 0
        # lower_index += 1
    else:
        lower_bound = -ops.kthvalue(-input, k=lower_index).values

    if not output_tensor:
        lower_bound = lower_bound.item()
        upper_bound = upper_bound.item()
    return lower_bound, upper_bound


def linear_quantize(input, scale, zero_point, inplace=False):
    """
    Quantize single-precision input tensor to integers with the given scaling factor and zeropoint.

    Args:
        input (`mindspore.Tensor`):
            Single-precision input tensor to be quantized.
        scale (`mindspore.Tensor`):
            Scaling factor for quantization.
        zero_pint (`mindspore.Tensor`):
            Shift for quantization.
        inplace (`bool`, *optional*, defaults to `False`):
            Whether to compute inplace or not.

    Returns:
        `mindspore.Tensor`: Linearly quantized value of *input* according to *scale* and *zero_point*.
    """
    # reshape scale and zeropoint for convolutional weights and activation
    if len(input.shape) == 4:
        scale = scale.view(-1, 1, 1, 1)
        zero_point = zero_point.view(-1, 1, 1, 1)
    # reshape scale and zeropoint for linear weights
    elif len(input.shape) == 2:
        scale = scale.view(-1, 1)
        zero_point = zero_point.view(-1, 1)
    else:
        scale = scale.view(-1)
        zero_point = zero_point.view(-1)
    # quantized = float / scale + zero_point
    if inplace:
        input.mul_(1.0 / scale).add_(zero_point).round_()
        return input
    return ops.round(1.0 / scale * input + zero_point)


def symmetric_linear_quantization_params(num_bits, saturation_min, saturation_max, per_channel=False):
    """
    Compute the scaling factor with the given quantization range for symmetric quantization.

    Args:
        saturation_min (`mindspore.Tensor`):
            Lower bound for quantization range.
        saturation_max (`mindspore.Tensor`):
            Upper bound for quantization range.
        per_channel (`bool`, *optional*, defaults to `False`):
            Whether to or not use channel-wise quantization.

    Returns:
        `mindspore.Tensor`: Scaling factor that linearly quantizes the given range between *saturation_min* and
        *saturation_max*.
    """
    # in this part, we do not need any gradient computation,
    # in order to enforce this, we put no_grad()
    with no_grad():
        n = 2 ** (num_bits - 1) - 1

        if per_channel:
            scale, _ = ops.max(ops.stack([saturation_min.abs(), saturation_max.abs()], dim=1), dim=1)
            scale = ops.clamp(scale, min=1e-8) / n

        else:
            scale = max(saturation_min.abs(), saturation_max.abs())
            scale = ops.clamp(scale, min=1e-8) / n

    return scale


class SymmetricQuantFunction(Cell):
    """
    Class to quantize the given floating-point values using symmetric quantization with given range and bitwidth.
    """

    def __init__(self, k, percentile_mode, scale):
        super().__init__()
        self.k = k
        self.percentile_mode = percentile_mode
        self.scale = scale

    def construct(self, x):
        """
        Args:
            x (`mindspore.Tensor`):
                Floating point tensor to be quantized.
            k (`int`):
                Quantization bitwidth.
            percentile_mode (`bool`):
                Whether or not to use percentile calibration.
            scale (`mindspore.Tensor`):
                Pre-calculated scaling factor for *x*. Note that the current implementation of SymmetricQuantFunction
                requires pre-calculated scaling factor.

        Returns:
            `mindspore.Tensor`: Symmetric-quantized value of *input*.
        """
        zero_point = mindspore.tensor(0.0)

        n = 2 ** (self.k - 1) - 1
        new_quant_x = linear_quantize(x, self.scale, zero_point, inplace=False)
        new_quant_x = ops.clamp(new_quant_x, -n, n - 1)

        return new_quant_x

    def bprop(self, x, out, dout):
        scale = self.scale

        if len(dout.shape) == 4:
            scale = scale.view(-1, 1, 1, 1)
        # reshape scale and zeropoint for linear weights
        elif len(dout.shape) == 2:
            scale = scale.view(-1, 1)
        else:
            scale = scale.view(-1)

        return (dout.copy() / scale,)

class floor_ste(Cell):
    """
    Straight-through Estimator(STE) for ops.floor()
    """
    def construct(self, x):
        return ops.floor(x)

    def bprop(self, x, out, dout):
        return (dout.copy(),)



class round_ste(Cell):
    """
    Straight-through Estimator(STE) for ops.round()
    """
    def construct(self, x):
        return ops.round(x)

    def bprop(self, x, out, dout):
        return (dout.copy(),)

def batch_frexp(inputs, max_bit=31):
    """
    Decompose the scaling factor into mantissa and twos exponent.

    Args:
        scaling_factor (`mindspore.Tensor`):
            Target scaling factor to decompose.

    Returns:
        ``Tuple(mindspore.Tensor, mindspore.Tensor)`: mantisa and exponent
    """

    shape_of_input = inputs.shape

    # trans the input to be a 1-d tensor
    inputs = inputs.view(-1)

    output_m, output_e = np.frexp(inputs.asnumpy())
    tmp_m = []
    for m in output_m:
        int_m_shifted = int(
            decimal.Decimal(m * (2**max_bit)).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP)
        )
        tmp_m.append(int_m_shifted)
    output_m = np.array(tmp_m)

    output_e = float(max_bit) - output_e

    return (
        mindspore.Tensor.from_numpy(output_m).view(shape_of_input),
        mindspore.Tensor.from_numpy(output_e).view(shape_of_input),
    )


class FixedPointMul(Cell):
    """
    Function to perform fixed-point arithmetic that can match integer arithmetic on hardware.

    Args:
        pre_act (`mindspore.Tensor`):
            Input tensor.
        pre_act_scaling_factor (`mindspore.Tensor`):
            Scaling factor of the input tensor *pre_act*.
        bit_num (`int`):
            Quantization bitwidth.
        z_scaling_factor (`mindspore.Tensor`):
            Scaling factor of the output tensor.
        identity (`mindspore.Tensor`, *optional*):
            Identity tensor, if exists.
        identity_scaling_factor (`mindspore.Tensor`, *optional*):
            Scaling factor of the identity tensor *identity*, if exists.

    Returns:
        `mindspore.Tensor`: Output tensor(*pre_act* if *identity* is not given, otherwise the addition of *pre_act* and
        *identity*), whose scale is rescaled to *z_scaling_factor*.
    """

    def __init__(self,
        pre_act_scaling_factor,
        bit_num,
        z_scaling_factor,
        identity_scaling_factor=None,
        ):
        super().__init__()
        self.pre_act_scaling_factor = pre_act_scaling_factor
        self.bit_num = bit_num
        self.z_scaling_factor = z_scaling_factor
        self.identity_scaling_factor = identity_scaling_factor

    def reshape_3d(self, x):
        return x

    def reshape_other(self, x):
        return x.view(1, 1, -1)

    def construct(
        self,
        pre_act,
        identity=None
    ):
        if len(self.pre_act_scaling_factor.shape) == 3:
            reshape = self.reshape_3d  # noqa: E731
        else:
            reshape = self.reshape_other  # noqa: E731
        self.identity = identity

        n = 2 ** (self.bit_num - 1) - 1

        self.pre_act_scaling_factor = reshape(self.pre_act_scaling_factor)
        if identity is not None:
            self.identity_scaling_factor = reshape(self.identity_scaling_factor)

        # self.z_scaling_factor = self.z_scaling_factor

        z_int = ops.round(pre_act / self.pre_act_scaling_factor)
        _A = self.pre_act_scaling_factor.type(mindspore.float64)
        _B = (self.z_scaling_factor.type(mindspore.float32)).type(mindspore.float64)
        new_scale = _A / _B
        new_scale = reshape(new_scale)

        m, e = batch_frexp(new_scale)

        output = z_int.type(mindspore.float64) * m.type(mindspore.float64)
        output = ops.round(output / (2.0**e))

        if identity is not None:
            # needs addition of identity activation
            wx_int = ops.round(identity / self.identity_scaling_factor)

            _A = self.identity_scaling_factor.type(mindspore.float64)
            _B = (self.z_scaling_factor.type(mindspore.float32)).type(mindspore.float64)
            new_scale = _A / _B
            new_scale = reshape(new_scale)

            m1, e1 = batch_frexp(new_scale)
            output1 = wx_int.type(mindspore.float64) * m1.type(mindspore.float64)
            output1 = ops.round(output1 / (2.0**e1))

            output = output1 + output

        return ops.clamp(output.type(mindspore.float32), -n - 1, n)

    def bprop(self, pre_act, identity, out, dout):
        identity_grad = ops.zeros_like(dout.copy())
        if self.identity is not None:
            identity_grad = dout.copy() / self.z_scaling_factor
        return dout.copy() / self.z_scaling_factor, identity_grad
