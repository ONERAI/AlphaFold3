# Copyright 2024 Ligo Biosciences Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Construct an initial 1D embedding."""
import torch
from torch import Tensor
from torch import nn
from torch.nn import functional as F
from torch.nn import LayerNorm
from src.models.components.atom_attention import AtomAttentionEncoder
from typing import Dict, NamedTuple, Tuple, Optional
from src.models.components.primitives import LinearNoBias, Linear
from src.models.components.relative_position_encoding import RelativePositionEncoding
from src.models.template import TemplatePairStack
from src.utils.tensor_utils import add
from src.utils.geometry.vector import Vec3Array
from src.utils.checkpointing import get_checkpoint_fn
checkpoint = get_checkpoint_fn()



class InputFeatureEmbedder(nn.Module):
    """A class that performs attention over all atoms in order to encode the information
    about the chemical structure of all the molecules, leading to a single representation
    representing all the tokens.
    - Embed per-atom features
    - Concatenate the per-token features
    TODO: during model training, this module is completely dead! 
        - encoder's output projection is learning
        - output_ln is learning
    """

    def __init__(
            self,
            c_token: int = 384,
            c_atom: int = 128,
            c_atompair: int = 16,
            c_trunk_pair: int = 128,
            no_blocks: int = 3,
            no_heads: int = 4,
            dropout=0.0,
            n_queries: int = 32,
            n_keys: int = 128,
    ):
        super().__init__()
        self.no_blocks = no_blocks
        self.c_token = c_token
        self.c_atom = c_atom
        self.c_atompair = c_atompair
        self.c_trunk_pair = c_trunk_pair
        self.no_heads = no_heads
        self.dropout = dropout
        self.n_queries = n_queries
        self.n_keys = n_keys

        # Atom Attention encoder
        self.encoder = AtomAttentionEncoder(
            c_token=self.c_token,
            c_atom=self.c_atom,
            c_atompair=self.c_atompair,
            c_trunk_pair=self.c_trunk_pair,
            no_blocks=self.no_blocks,
            no_heads=self.no_heads,
            dropout=self.dropout,
            n_queries=self.n_queries,
            n_keys=self.n_keys,
            trunk_conditioning=False,  # no trunk conditioning for the input feature embedder
        )

        # Output projection
        self.restype_embedding = LinearNoBias(21, c_token)  # for the restype embedding
        self.output_ln = LayerNorm(c_token)

    def forward(
            self,
            features: Dict[str, Tensor],
            n_tokens: int,
            mask: Tensor = None
    ) -> Tensor:
        """Forward pass of the input feature embedder.
        Args:
            features:
                Dictionary containing the input features:
                    "ref_pos":
                        [*, N_atoms, 3] atom positions in the reference conformers, with
                        a random rotation and translation applied. Atom positions in Angstroms.
                    "ref_charge":
                        [*, N_atoms] Charge for each atom in the reference conformer.
                    "ref_mask":
                        [*, N_atoms] Mask indicating which atom slots are used in the reference
                        conformer.
                    "ref_element":
                        [*, N_atoms, 128] One-hot encoding of the element atomic number for each atom
                        in the reference conformer, up to atomic number 128.
                    "ref_atom_name_chars":
                        [*, N_atom, 4, 64] One-hot encoding of the unique atom names in the reference
                        conformer. Each character is encoded as ord(c - 32), and names are padded to
                        length 4.
                    "ref_space_uid":
                        [*, N_atoms] Numerical encoding of the chain id and residue index associated
                        with this reference conformer. Each (chain id, residue index) tuple is assigned
                        an integer on first appearance.
                    "aatype":
                        [*, N_token] Amino acid type for each token.
                    "atom_to_token":
                        [*, N_atoms] Token index for each atom in the flat atom representation.
            n_tokens:
                number of tokens
            mask:
                [*, N_atoms] mask indicating which atoms are valid (non-padding).
        Returns:
            [*, N_tokens, c_token] Embedding of the input features.
        """
        # Encode the input features
        output = self.encoder(features=features, mask=mask, n_tokens=n_tokens)
        per_token_features = output.token_single.squeeze(-3)  # remove the samples_per_trunk dimension
        f_restype = F.one_hot(features["aatype"], num_classes=21).to(per_token_features.dtype)
        per_token_features = per_token_features + self.restype_embedding(f_restype)
        per_token_features = self.output_ln(per_token_features)
        return per_token_features


class InputEmbedder(nn.Module):
    """Input embedder for AlphaFold3 that initializes the single and pair representations."""

    def __init__(
            self,
            c_token: int = 384,
            c_atom: int = 128,
            c_atompair: int = 16,
            c_trunk_pair: int = 128,
    ):
        super(InputEmbedder, self).__init__()

        # InputFeatureEmbedder for the s_inputs representation
        self.input_feature_embedder = InputFeatureEmbedder(
            c_token=c_token,
            c_atom=c_atom,
            c_atompair=c_atompair,
            c_trunk_pair=c_trunk_pair
        )

        # Projections
        self.linear_single = LinearNoBias(c_token, c_token)
        self.linear_proj_i = LinearNoBias(c_token, c_trunk_pair)
        self.linear_proj_j = LinearNoBias(c_token, c_trunk_pair)
        # self.linear_bonds = LinearNoBias(1, c_trunk_pair)

        # Relative position encoding
        self.relpos = RelativePositionEncoding(c_pair=c_trunk_pair)

    def forward(
            self,
            features: Dict[str, Tensor],
            inplace_safe: bool = False,
    ) -> Tuple[Tensor, Tensor, Tensor]:
        """
        Args:
            features:
                Dictionary containing the following input features:
                    "ref_pos" ([*, N_atoms, 3]):
                        atom positions in the reference conformers, with
                        a random rotation and translation applied. Atom positions in Angstroms.
                    "ref_charge" ([*, N_atoms]):
                        Charge for each atom in the reference conformer.
                    "ref_mask" ([*, N_atoms]):
                        Mask indicating which atom slots are used in the reference
                        conformer.
                    "ref_element" ([*, N_atoms, 128]):
                        One-hot encoding of the element atomic number for each atom
                        in the reference conformer, up to atomic number 128.
                    "ref_atom_name_chars" ([*, N_atom, 4, 64]):
                        One-hot encoding of the unique atom names in the reference
                        conformer. Each character is encoded as ord(c - 32), and names are padded to
                        length 4.
                    "ref_space_uid" ([*, N_atoms]):
                        Numerical encoding of the chain id and residue index associated
                        with this reference conformer. Each (chain id, residue index) tuple is assigned
                        an integer on first appearance.
                    "atom_to_token" ([*, N_atoms]):
                        Token index for each atom in the flat atom representation.
                    "atom_mask" ([*, n_atoms]):
                        mask indicating which atoms are valid (non-padding).
                    "token_mask" ([*, N_token]):
                        mask indicating which tokens are valid (non-padding).
            inplace_safe:
                whether to use inplace operations
        """
        *_, n_tokens = features["token_mask"].shape

        # Extract masks
        atom_mask = features["atom_mask"]
        token_mask = features["token_mask"]

        # Single representation with input feature embedder
        s_inputs = self.input_feature_embedder(
            features,
            n_tokens=n_tokens,
            mask=atom_mask,
        )

        # Projections
        s_init = self.linear_single(s_inputs)
        z_init = add(
            self.linear_proj_i(s_inputs[..., None, :]),
            self.linear_proj_j(s_inputs[..., None, :, :]),
            inplace=False  # inplace_safe
        )  # (*, n_tokens, n_tokens, c_trunk_pair)

        # Add relative position encoding
        z_init = add(
            z_init,
            self.relpos(features, mask=token_mask),
            inplace=inplace_safe
        )

        # Add token bond information
        # z_init = add(
        #    z_init,
        #    self.linear_bonds(features["token_bonds"][..., None]),
        #    inplace=inplace_safe
        # )
        return s_inputs, s_init, z_init


# Template Embedder #

def dgram_from_positions(
        pos: torch.Tensor,
        min_bin: float = 3.25,
        max_bin: float = 50.75,
        no_bins: int = 39,
        inf: float = 1e8,
):
    """Computes a distogram given a position tensor."""
    dgram = torch.sum(
        (pos[..., None, :] - pos[..., None, :, :]) ** 2, dim=-1, keepdim=True
    )
    lower = torch.linspace(min_bin, max_bin, no_bins, device=pos.device) ** 2
    upper = torch.cat([lower[1:], lower.new_tensor([inf])], dim=-1)
    dgram = ((dgram > lower) * (dgram < upper)).type(dgram.dtype)

    return dgram


class TemplateEmbedder(nn.Module):
    def __init__(
            self,
            no_blocks: int = 2,
            c_template: int = 64,
            c_z: int = 128,
            clear_cache_between_blocks: bool = False
    ):
        super(TemplateEmbedder, self).__init__()

        self.proj_pair = nn.Sequential(
            LayerNorm(c_z),
            LinearNoBias(c_z, c_template)
        )
        no_template_features = 84  # 108
        self.linear_templ_feat = LinearNoBias(no_template_features, c_template)
        self.pair_stack = TemplatePairStack(
            no_blocks=no_blocks,
            c_template=c_template,
            clear_cache_between_blocks=clear_cache_between_blocks
        )
        self.v_to_u_ln = LayerNorm(c_template)
        self.output_proj = nn.Sequential(
            nn.ReLU(),
            LinearNoBias(c_template, c_z)
        )
        self.clear_cache_between_blocks = clear_cache_between_blocks

    def forward(
            self,
            features: Dict[str, Tensor],
            z_trunk: Tensor,
            pair_mask: Tensor,
            chunk_size: Optional[int] = None,
            use_deepspeed_evo_attention: bool = False,
            inplace_safe: bool = False,
    ) -> Tensor:
        """
        TODO: modify this function to take the same features as the OpenFold template embedder. That will allow
         minimal changes in the data pipeline.
        Args:
            features:
                Dictionary containing the template features:
                    "template_aatype":
                        [*, N_templ, N_token, 32] One-hot encoding of the template sequence.
                    "template_pseudo_beta":
                        [*, N_templ, N_token, 3] coordinates of the representative atoms
                    "template_pseudo_beta_mask":
                        [*, N_templ, N_token] Mask indicating if the Cβ (Cα for glycine)
                        has coordinates for the template at this residue.
                    "asym_id":
                        [*, N_token] Unique integer for each distinct chain.
            z_trunk:
                [*, N_token, N_token, c_z] pair representation from the trunk.
            pair_mask:
                [*, N_token, N_token] mask indicating which pairs are valid (non-padding).
            chunk_size:
                Chunk size for the pair stack.
            use_deepspeed_evo_attention:
                Whether to use DeepSpeed Evo attention within the pair stack.
            inplace_safe:
                Whether to use inplace operations.
        """
        # Grab data about the inputs
        bs, n_templ, n_token = features["template_aatype"].shape

        # Compute template distogram
        template_distogram = dgram_from_positions(features["template_pseudo_beta"])

        # Compute the unit vector
        # pos = Vec3Array.from_array(features["template_pseudo_beta"])
        # template_unit_vector = (pos / pos.norm()).to_tensor().to(template_distogram.dtype)
        # print(f"template_unit_vector shape: {template_unit_vector.shape}")

        # One-hot encode template restype
        template_restype = F.one_hot(  # [*, N_templ, N_token, 22]
            features["template_aatype"],
            num_classes=22  # 20 amino acids + UNK + gap
        ).to(template_distogram.dtype)

        # TODO: add template backbone frame feature

        # Compute masks
        # b_frame_mask = features["template_backbone_frame_mask"]
        # b_frame_mask = b_frame_mask[..., None] * b_frame_mask[..., None, :]  # [*, n_templ, n_token, n_token]
        b_pseudo_beta_mask = features["template_pseudo_beta_mask"]
        b_pseudo_beta_mask = b_pseudo_beta_mask[..., None] * b_pseudo_beta_mask[..., None, :]

        template_feat = torch.cat([
            template_distogram,
            # b_frame_mask[..., None],  # [*, n_templ, n_token, n_token, 1]
            # template_unit_vector,
            b_pseudo_beta_mask[..., None]
        ], dim=-1)

        # Mask out features that are not in the same chain
        asym_id_i = features["asym_id"][..., None, :].expand((bs, n_token, n_token))
        asym_id_j = features["asym_id"][..., None].expand((bs, n_token, n_token))
        same_asym_id = torch.isclose(asym_id_i, asym_id_j).to(template_feat.dtype)  # [*, n_token, n_token]
        same_asym_id = same_asym_id.unsqueeze(-3)  # for N_templ broadcasting
        template_feat = template_feat * same_asym_id.unsqueeze(-1)

        # Add residue type information
        temp_restype_i = template_restype[..., None, :].expand((bs, n_templ, n_token, n_token, -1))
        temp_restype_j = template_restype[..., None, :, :].expand((bs, n_templ, n_token, n_token, -1))
        template_feat = torch.cat([template_feat, temp_restype_i, temp_restype_j], dim=-1)

        # Mask the invalid features
        template_feat = template_feat * b_pseudo_beta_mask[..., None]

        # Run the pair stack per template
        single_templates = torch.unbind(template_feat, dim=-4)  # each element shape [*, n_token, n_token, no_feat]
        z_proj = self.proj_pair(z_trunk)
        u = torch.zeros_like(z_proj)
        for t in range(len(single_templates)):
            # Project and add the template features
            v = z_proj + self.linear_templ_feat(single_templates[t])
            # Run the pair stack
            v = add(v,
                    self.pair_stack(v,
                                    pair_mask=pair_mask,
                                    chunk_size=chunk_size,
                                    use_deepspeed_evo_attention=use_deepspeed_evo_attention,
                                    inplace_safe=inplace_safe),
                    inplace=inplace_safe
                    )
            # Normalize and add to u
            u = add(u, self.v_to_u_ln(v), inplace=inplace_safe)
            del v
        u = torch.div(u, n_templ)  # average
        u = self.output_proj(u)
        return u
