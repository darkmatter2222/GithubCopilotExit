from transformers.modeling_gguf_pytorch_utils import load_gguf_checkpoint
import sys

path = "/usr/share/ollama/.ollama/models/blobs/sha256-415aad607fc297a2ad84cda238febbde295f49f17c17ad7784e2246060fb06d0"
try:
    result = load_gguf_checkpoint(path, return_tensors=False)
    print("SUCCESS: model_type =", result["config"].get("model_type"))
    print("architecture key:", result["config"].get("architectures"))
except Exception as e:
    print("ERROR:", e)
