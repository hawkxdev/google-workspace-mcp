# Maintenance dynamic routing for Hawkx Workspace MCP services.
# Included via /etc/nginx/snippets/google-workspace-mcp-dynamic.inc.

location /.well-known/oauth-protected-resource/ {
    add_header Retry-After 300 always;
    return 503;
}

location /.well-known/oauth-authorization-server/ {
    add_header Retry-After 300 always;
    return 503;
}

location /gmail/ {
    add_header Retry-After 300 always;
    return 503;
}

location /calendar/ {
    add_header Retry-After 300 always;
    return 503;
}

location /drive/ {
    add_header Retry-After 300 always;
    return 503;
}

location /sheets/ {
    add_header Retry-After 300 always;
    return 503;
}

location /docs/ {
    add_header Retry-After 300 always;
    return 503;
}
