# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
"""Inference-only BaiChuan model compatible with HuggingFace weights.

The input of the model is flattened to a 1D tensor of tokens. The model uses
InputMetadata to extract the original 2D shape of the input.
"""
import math
from typing import List, Optional, Tuple

import torch
from torch import nn

from vllm.model_executor.layers.activation import SiluAndMul
from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.layernorm import RMSNorm
from vllm.model_executor.layers.attention import (
    PagedAttentionWithRoPE,
    PagedAttentionWithALiBi,
)
from vllm.model_executor.layers.sampler import Sampler
from vllm.model_executor.weight_utils import (
    convert_pyslice_to_tensor,
    hf_model_weights_iterator,
    load_padded_tensor_parallel_vocab,
    load_tensor_parallel_weights,
)
from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
)
from vllm.model_executor.parallel_utils.layers import (
    VocabParallelEmbedding,
    ColumnParallelLinear,
    RowParallelLinear,
)
from vllm.sequence import SamplerOutput
from vllm.transformers_utils.configs.baichuan2 import BaiChuan2Config as BaiChuanConfig

KVCache = Tuple[torch.Tensor, torch.Tensor]


def _get_interleave(n):

    def _get_interleave_power_of_2(n):
        start = 2**(-(2**-(math.log2(n) - 3)))
        ratio = start
        return [start * ratio**i for i in range(n)]

    if math.log2(n).is_integer():
        return _get_interleave_power_of_2(n)
    else:
        closest_power_of_2 = 2**math.floor(math.log2(n))
        return (_get_interleave_power_of_2(closest_power_of_2) +
                _get_interleave(
                    2 * closest_power_of_2)[0::2][:n - closest_power_of_2])


def _get_alibi_slopes(total_num_heads: int) -> torch.Tensor:
    slopes = torch.Tensor(_get_interleave(total_num_heads))
    return slopes


class MLP(torch.nn.Module):

    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        hidden_act: str,
    ):
        super().__init__()
        self.gate_up_proj = ColumnParallelLinear(
            hidden_size,
            2 * intermediate_size,
            bias=False,
            gather_output=False,
        )
        self.down_proj = RowParallelLinear(
            intermediate_size,
            hidden_size,
            bias=False,
            input_is_parallel=True,
        )
        if hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {hidden_act}. "
                             "Only silu is supported for now.")
        self.act_fn = SiluAndMul()

    def forward(self, x):
        gate_up, _ = self.gate_up_proj(x)
        x = self.act_fn(gate_up)
        x, _ = self.down_proj(x)
        return x


class BaichuanAttention(torch.nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        position_embedding: str,
    ):
        super().__init__()
        self.hidden_size = hidden_size
        tensor_model_parallel_world_size = get_tensor_model_parallel_world_size(
        )
        self.total_num_heads = num_heads
        assert self.total_num_heads % tensor_model_parallel_world_size == 0
        self.num_heads = self.total_num_heads // tensor_model_parallel_world_size
        self.head_dim = hidden_size // self.total_num_heads
        self.postion_embedding = position_embedding

        # pylint: disable=invalid-name
        self.W_pack = ColumnParallelLinear(
            hidden_size,
            3 * hidden_size,
            bias=False,
            gather_output=False,
        )
        self.o_proj = RowParallelLinear(
            self.total_num_heads * self.head_dim,
            hidden_size,
            bias=False,
            input_is_parallel=True,
        )
        # Create the alibi slopes and slice them.
        if self.postion_embedding == "ALIBI":
            tp_rank = get_tensor_model_parallel_rank()
            head_start = tp_rank * self.num_heads
            head_end = (tp_rank + 1) * self.num_heads
            alibi_slopes = _get_alibi_slopes(self.total_num_heads)
            alibi_slopes = alibi_slopes[head_start:head_end].tolist()

            scaling = self.head_dim**-0.5
            self.attn = PagedAttentionWithALiBi(self.num_heads, self.head_dim,
                                                scaling, alibi_slopes)
        else:
            self.scaling = self.head_dim**-0.5
            self.attn = PagedAttentionWithRoPE(self.num_heads,
                                               self.head_dim,
                                               self.scaling,
                                               rotary_dim=self.head_dim)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:
        qkv, _ = self.W_pack(hidden_states)
        q, k, v = qkv.chunk(chunks=3, dim=-1)
        k_cache, v_cache = kv_cache
        if self.postion_embedding == "ALIBI":
            attn_output = self.attn(q, k, v, k_cache, v_cache, input_metadata,
                                    cache_event)
        else:
            attn_output = self.attn(positions, q, k, v, k_cache, v_cache,
                                    input_metadata, cache_event)

        output, _ = self.o_proj(attn_output)
        return output


class BaichuanLayer(torch.nn.Module):

    def __init__(self, config: BaiChuanConfig, position_embedding: str):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = BaichuanAttention(
            hidden_size=self.hidden_size,
            num_heads=config.num_attention_heads,
            position_embedding=position_embedding,
        )
        self.mlp = MLP(
            hidden_size=self.hidden_size,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
        )
        self.input_layernorm = RMSNorm(config.hidden_size,
                                       eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size,
                                                eps=config.rms_norm_eps)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:
        # Self Attention
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)
        # Self Attention
        hidden_states = self.self_attn(
            positions=positions,
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            input_metadata=input_metadata,
            cache_event=cache_event,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states
        return hidden_states


class BaichuanModel(nn.Module):

    def __init__(self, config: BaiChuanConfig, position_embedding: str):
        super().__init__()
        self.config = config
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size
        self.embed_tokens = VocabParallelEmbedding(
            config.vocab_size,
            config.hidden_size,)
        self.layers = nn.ModuleList([
            BaichuanLayer(config, position_embedding)
            for _ in range(config.num_hidden_layers)
        ])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> torch.Tensor:
        hidden_states = self.embed_tokens(input_ids)
        for i in range(len(self.layers)):
            if cache_events is None:
                cache_event = None
            else:
                cache_event = cache_events[i]
            layer = self.layers[i]
            hidden_states = layer(
                positions,
                hidden_states,
                kv_caches[i],
                input_metadata,
                cache_event,
            )
        hidden_states = self.norm(hidden_states)
        return hidden_states


class BaiChuanBaseForCausalLM(nn.Module):

    def __init__(self, config, position_embedding: str):
        super().__init__()
        self.config = config
        self.model = BaichuanModel(config, position_embedding)
        self.lm_head = ColumnParallelLinear(
            config.hidden_size,
            config.vocab_size,
            bias=False,
            gather_output=False,
        )
        self.sampler = Sampler(config.vocab_size)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> SamplerOutput:
        hidden_states = self.model(input_ids, positions, kv_caches,
                                   input_metadata, cache_events)
        next_tokens = self.sampler(self.lm_head.weight, hidden_states,
                                   input_metadata)
        return next_tokens

    _column_parallel_weights = []
    _row_parallel_weights = ["o_proj.weight", "down_proj.weight"]

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        tp_world_size = get_tensor_model_parallel_world_size()
        tp_rank = get_tensor_model_parallel_rank()
        state_dict = self.state_dict()
        has_norm_head = False

        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "rotary_emb.inv_freq" in name:
                continue

            loaded_weight = convert_pyslice_to_tensor(loaded_weight)

            if "W_pack" in name:
                total_num_heads = self.config.num_attention_heads
                hidden_size = self.config.hidden_size
                head_size = hidden_size // total_num_heads
                num_heads = total_num_heads // tp_world_size
                head_start = tp_rank * num_heads
                head_end = (tp_rank + 1) * num_heads

                loaded_weight = loaded_weight.view(3, total_num_heads,
                                                   head_size, hidden_size)
                loaded_weight = loaded_weight[:, head_start:head_end, :, :]
                loaded_weight = loaded_weight.reshape(-1, hidden_size)

            is_gate_up_weight = False
            for stride_id, weight_name in enumerate(["gate_proj", "up_proj"]):
                if weight_name not in name:
                    continue
                param = state_dict[name.replace(weight_name, "gate_up_proj")]
                shard_size = param.shape[0] // 2
                loaded_weight = loaded_weight[shard_size * tp_rank:shard_size *
                                              (tp_rank + 1)]
                param_slice = param.data[shard_size * stride_id:shard_size *
                                         (stride_id + 1)]
                assert param_slice.shape == loaded_weight.shape
                param_slice.copy_(loaded_weight)
                is_gate_up_weight = True
                break
            if is_gate_up_weight:
                continue

            param = state_dict[name]

            if name == "lm_head.weight":
                # print(
                #     f"loading lm_head weight, norm: {loaded_weight.norm(2.0, 1, True).clamp_min(1e-12)}, shape: {loaded_weight.size()}"
                # )
                if loaded_weight.dtype == torch.float16 and loaded_weight.device == torch.device("cpu"):
                    loaded_weight = torch.nn.functional.normalize(loaded_weight.float()).half()
                else:
                    loaded_weight = torch.nn.functional.normalize(loaded_weight)
                # print(
                #     f"after normalization, norm: {loaded_weight.norm(2.0, 1, True).clamp_min(1e-12)}, shape: {loaded_weight.size()}"
                # )
                has_norm_head = True

            if "embed_tokens" in name or "lm_head" in name:
                load_padded_tensor_parallel_vocab(param, loaded_weight,
                                                  tp_rank)
                continue

            load_tensor_parallel_weights(
                param,
                loaded_weight,
                name,
                self._column_parallel_weights,
                self._row_parallel_weights,
                tp_rank,
            )
        assert has_norm_head, "lm_head silently evades normalization"


class Baichuan2ForCausalLM(BaiChuanBaseForCausalLM):  # baichuan 13b

    def __init__(self, config):
        super().__init__(config, "ALIBI")


class BaiChuan2ForCausalLM(BaiChuanBaseForCausalLM):  # baichuan 7b

    def __init__(self, config):
        super().__init__(config, "ROPE")
