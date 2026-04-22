---
title: Documentation Home
author: inact demo
---
# Documentation

Welcome to the example documentation folder.

## Pages

- [Guide](guide.md) — step-by-step guide
- [Config](config.toml) — configuration reference

## Navigation

Agents can browse this folder programmatically:

```
GET /docs/.ls               list all files
GET /docs/.grep?q=keyword   search content
GET /docs/README.md         this file (plain text)
```

Humans can visit `/_human/docs/README.md` for a rendered view.
