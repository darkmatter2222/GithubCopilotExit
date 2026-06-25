from transformers.modeling_gguf_pytorch_utils import GGUF_SUPPORTED_ARCHITECTURES
qwen_archs = [a for a in GGUF_SUPPORTED_ARCHITECTURES if "qwen" in a.lower()]
print("Supported qwen GGUF architectures:", qwen_archs)
