from typing import Tuple
import torch
from lmql.models.lmtp.backends.lmtp_model import LMTPModel, LMTPModelResult, TokenStreamer
import numpy as np

def format_call(model_name, **kwargs):
    if len(kwargs) == 0:
        return f'"{model_name}"'
    return f'"{model_name}", {", ".join([f"{k}={v}" for k, v in kwargs.items()])}'

class TransformersLLM(LMTPModel):
    def __init__(self, model_identifier, **kwargs):
        self.model_identifier = model_identifier
        self.model_args = kwargs

        self.max_batch_size = kwargs.pop("batch_size", 32)

        self.silent = kwargs.pop("silent", False)

        if self.model_args.pop("loader", None) == "auto-gptq":
            from auto_gptq import AutoGPTQForCausalLM
            if not self.silent:
                print("[Loading", self.model_identifier, "with", "AutoGPTQForCausalLM.from_quantized({})]".format(format_call(self.model_identifier, **self.model_args)), flush=True)
            
            self.model = AutoGPTQForCausalLM.from_quantized(self.model_identifier, **self.model_args)
        else:
            from transformers import AutoModelForCausalLM
            if not self.silent:
                print("[Loading", self.model_identifier, "with", "AutoModelForCausalLM.from_pretrained({})]".format(format_call(self.model_identifier, **self.model_args)), flush=True)
            
            self.model = AutoModelForCausalLM.from_pretrained(self.model_identifier, **self.model_args)
        
        if not self.silent:
            print("[", self.model_identifier, " ready on device ", self.model.device, 
        flush=True, sep="", end="]\n")

    @property
    def eos_token_id(self):
        return self.model.config.eos_token_id

    def score(self, input_ids: torch.LongTensor, attention_mask: torch.LongTensor, **model_kwargs) -> Tuple[torch.FloatTensor, torch.FloatTensor]:
        input_ids = torch.tensor(input_ids).to(self.model.device)
        attention_mask = torch.tensor(attention_mask).to(self.model.device)
        
        # prepare model inputs
        model_inputs = self.model.prepare_inputs_for_generation(input_ids, **model_kwargs, attention_mask=attention_mask, eos_token_id=self.eos_token_id)
        model_inputs["attention_mask"] = attention_mask

        token_scores = []
        
        outputs = self.model(
            **model_inputs,
            return_dict=True,
            output_attentions=False,
            output_hidden_states=False,
        )

        next_token_logits = outputs.logits[:, :-1, :]
        next_token_logits = torch.log_softmax(next_token_logits, dim=-1)
        token_scores = next_token_logits.gather(-1, input_ids[:,1:].unsqueeze(-1))

        return np.array([[0.0] + scores.flatten().tolist() for scores in token_scores])
    
    def generate(self, input_ids: torch.LongTensor, attention_mask: torch.LongTensor, 
                 temperature: float, max_new_tokens: int, 
                 bias_tensor: torch.FloatTensor, streamer: TokenStreamer) -> LMTPModelResult:
        input_ids = torch.tensor(input_ids).to(self.model.device)
        attention_mask = torch.tensor(attention_mask).to(self.model.device)
        
        kwargs = {
            "input_ids": input_ids,
            "do_sample": temperature > 0.0,
            "attention_mask": attention_mask,
            "temperature": temperature,
            "max_new_tokens": max_new_tokens,
            "logits_processor": self.logits_processors(bias_tensor),
            "output_scores": True,
            "return_dict_in_generate": True
        }

        result = self.model.generate(**kwargs, stopping_criteria=[TokenStreamerDisguisedAsStoppingCriterion(streamer)], 
                                     eos_token_id=self.eos_token_id, pad_token_id=self.eos_token_id)

        return LMTPModelResult(sequences=result.sequences, scores=result.scores)
    
    def logits_processors(self, logit_biases):
        bias_tensors = None
        make_bias_tensor = self.make_bias_tensor
        
        if len(logit_biases) == 0:
            return []

        class BatchLogitsProcessor:
            def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
                nonlocal bias_tensors

                if bias_tensors is None:
                    bias_tensors = torch.tensor(make_bias_tensor(logit_biases, scores.shape[-1])).to(scores.device)

                return torch.log_softmax(scores + bias_tensors, dim=-1)

        return [BatchLogitsProcessor()]

class TokenStreamerDisguisedAsStoppingCriterion:
    def __init__(self, token_streamer: TokenStreamer):
        self.token_streamer = token_streamer

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> bool:
        self.token_streamer(input_ids, scores, **kwargs)
        return False

LMTPModel.registry["transformers"] = TransformersLLM
