#!/usr/bin/env python3
with open('/etc/nginx/conf.d/default.conf', 'r') as f:
    lines = f.readlines()

# Find and replace each bad line
for i, line in enumerate(lines):
    stripped = line.rstrip('\n')
    if stripped == '        proxy_set_header X-Real-IP ;':
        lines[i] = '        proxy_set_header X-Real-IP $remote_addr;\n'
    elif stripped == '        proxy_set_header X-Forwarded-For ;':
        lines[i] = '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
    elif stripped == '        proxy_set_header X-Forwarded-Proto ;':
        lines[i] = '        proxy_set_header X-Forwarded-Proto $scheme;\n'

with open('/etc/nginx/conf.d/default.conf', 'w') as f:
    f.writelines(lines)

print('Fixed.', flush=True)
