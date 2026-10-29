---
name: linear-issues
description: Open Linear issues assigned to you. Find linear_digest with tool_search and call it for the morning list.
---

# Linear issues

When someone asks which Linear issues are assigned to them, which are due, or what the morning list says, call `linear_digest` with an empty argument object.

The tool only reads. It does not create, edit, or comment on issues. It returns the same text the morning script prints. An empty result means no open issue is assigned to this key. A one-line failure means the list was not read.

Do not pass the API key as an argument. The key stays in this profile's `.env` as `LINEAR_API_KEY`.
