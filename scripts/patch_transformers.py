filepath = "/home/darkmatter2222/vllm-env/lib/python3.12/site-packages/transformers/modeling_gguf_pytorch_utils.py"
with open(filepath, "r") as f:
    content = f.read()

old = '    elif "qwen3moe" in architecture:\n        updated_architecture = "qwen3_moe"'
new = '    elif "qwen35" in architecture:\n        updated_architecture = "qwen3"\n    elif "qwen3moe" in architecture:\n        updated_architecture = "qwen3_moe"'

if old in content:
    content = content.replace(old, new, 1)
    with open(filepath, "w") as f:
        f.write(content)
    print("Patched successfully")
else:
    print("Pattern not found!")
    # Show context around qwen3moe
    for i, line in enumerate(content.splitlines()):
        if "qwen3moe" in line:
            print(f"Line {i+1}: {line}")
