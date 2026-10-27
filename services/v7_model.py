import torch
import torch.nn as nn
from transformers import AutoModel, AutoTokenizer


class MultiHeadMiniLM_v7(nn.Module):
    def __init__(self, model_name="sentence-transformers/all-MiniLM-L6-v2"):
        super().__init__()
        self.backbone = AutoModel.from_pretrained(model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)

        self.shared = nn.Sequential(nn.Linear(1152, 512), nn.ReLU(), nn.Dropout(0.2))

        self.head_obj = nn.Linear(512, 1)
        self.head_meth = nn.Linear(512, 1)
        self.head_num = nn.Linear(512, 1)
        self.head_claim = nn.Linear(512, 1)
        self.head_overall = nn.Linear(512, 1)
        self.head_weight = nn.Linear(512, 1)
        self.sigmoid = nn.Sigmoid()

    def mean_pooling(self, model_output, attention_mask):
        token_embeddings = model_output[0]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)

    def forward(self, input_ids_a, mask_a, input_ids_b, mask_b):
        out_a = self.backbone(input_ids=input_ids_a, attention_mask=mask_a)
        embed_a = self.mean_pooling(out_a, mask_a)

        out_b = self.backbone(input_ids=input_ids_b, attention_mask=mask_b)
        embed_b = self.mean_pooling(out_b, mask_b)

        features = torch.cat([embed_a, embed_b, torch.abs(embed_a - embed_b)], dim=1)
        shared_out = self.shared(features)

        return {
            "objectives_alignment": self.sigmoid(self.head_obj(shared_out)).squeeze(-1),
            "methodology_fit": self.sigmoid(self.head_meth(shared_out)).squeeze(-1),
            "numeric_consistency": self.sigmoid(self.head_num(shared_out)).squeeze(-1),
            "claim_support": self.sigmoid(self.head_claim(shared_out)).squeeze(-1),
            "overall_coherence": self.sigmoid(self.head_overall(shared_out)).squeeze(-1),
            "pair_weight": self.sigmoid(self.head_weight(shared_out)).squeeze(-1)
        }
