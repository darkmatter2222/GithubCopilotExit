#!/bin/bash
# Fix all corrupted lines in the nginx config where PowerShell ate $vars
sed -i 's/proxy_set_header Host .*/proxy_set_header Host $host;/' /etc/nginx/conf.d/default.conf
sed -i '/proxy_set_header X-Real-IP;/c\        proxy_set_header X-Real-IP $remote_addr;' /etc/nginx/conf.d/default.conf
sed -i '/proxy_set_header X-Forwarded-For;/c\        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' /etc/nginx/conf.d/default.conf
sed -i '/proxy_set_header X-Forwarded-Proto;/c\        proxy_set_header X-Forwarded-Proto $scheme;' /etc/nginx/conf.d/default.conf
nginx -t
