# Powdrr Workspace UI

The browser client is a React and TypeScript application built with Vite. It
talks only to the local workspace API; provider configuration and workflow
execution remain in the Python application.

## Development

Start a local API on `http://127.0.0.1:8765`, then run:

```sh
npm install
npm run dev
```

The development server proxies `/api` requests to the local API and forwards the
configured loopback origin. Set `POWDRR_WORKSPACE_API` to change the target. The
first browser visit asks for the one-time workspace launch token. After sign-in,
the HttpOnly session cookie survives reloads; the client obtains a fresh CSRF
token from the same-origin session refresh route and keeps that token in memory.

## Checks

```sh
npm run typecheck
npm run lint
npm test
npm run build
```

The frontend DTOs mirror the versioned records in
`packages/contracts/src/powdrr_contracts/workspace.py`. They include only the
records used by this UI increment; job-specific rendering is added with the job
panel.
