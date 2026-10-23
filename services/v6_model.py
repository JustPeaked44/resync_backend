import torch
import torch.nn as nn
from transformers import AutoModel


class MultiHeadMiniLM(nn.Module):
    def __init__(self):
        super(MultiHeadMiniLM, self).__init__()
        self.backbone = AutoModel.from_pretrained("./models/model_v5b", local_files_only=True)
        self.shared = nn.Linear(1152, 512)
        self.relu = nn.ReLU()
        self.objectives_alignment = nn.Linear(512, 1)
        self.methodology_fit = nn.Linear(512, 1)
        self.numeric_consistency = nn.Linear(512, 1)
        self.claim_support = nn.Linear(512, 1)
        self.overall_coherence = nn.Linear(512, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, input_ids_a, mask_a, input_ids_b, mask_b):
        out_a = self.backbone(input_ids_a, attention_mask=mask_a).last_hidden_state.mean(dim=1)
        out_b = self.backbone(input_ids_b, attention_mask=mask_b).last_hidden_state.mean(dim=1)
        diff = torch.abs(out_a - out_b)
        combined = torch.cat([out_a, out_b, diff], dim=1)
        x = self.relu(self.shared(combined))
        return {
            "objectives_alignment": self.sigmoid(self.objectives_alignment(x)),
            "methodology_fit": self.sigmoid(self.methodology_fit(x)),
            "numeric_consistency": self.sigmoid(self.numeric_consistency(x)),
            "claim_support": self.sigmoid(self.claim_support(x)),
            "overall_coherence": self.sigmoid(self.overall_coherence(x)),
        }
