{
if ($0 == "        proxy_set_header X-Real-IP ;") print "        proxy_set_header X-Real-IP $remote_addr;";
else if ($0 == "        proxy_set_header X-Forwarded-For ;") print "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;";
else if ($0 == "        proxy_set_header X-Forwarded-Proto ;") print "        proxy_set_header X-Forwarded-Proto $scheme;";
else print
}
