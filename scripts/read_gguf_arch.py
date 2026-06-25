import gguf, sys

for path, label in [
    ("/usr/share/ollama/.ollama/models/blobs/sha256-415aad607fc297a2ad84cda238febbde295f49f17c17ad7784e2246060fb06d0", "qwen3-main"),
    ("/usr/share/ollama/.ollama/models/blobs/sha256-1194192cf2a187eb02722edcc3f77b11d21f537048ce04b67ccf8ba78863006a", "qwen3-coder"),
]:
    try:
        r = gguf.GGUFReader(path)
        for key in ["general.architecture", "general.name", "general.basename", "general.organization"]:
            field = r.fields.get(key)
            if field:
                val = bytes(field.parts[-1]).decode("utf-8", errors="replace")
                print(f"{label} {key}: {val}")
    except Exception as e:
        print(f"{label} ERROR: {e}")
