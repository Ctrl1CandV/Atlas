# Atlas web application

This directory contains the React/TypeScript observer bundled with Atlas. It is not released separately.

From a Windows PowerShell prompt:

```powershell
cd web
npm ci
npm run lint
npm run test:diff
npm run build
```

The production build is written to `web/dist` and served by `python -m atlas.web`. For development, run `npm run dev`; API-backed features still require the Atlas Python service.

See the repository-level `README.md` for supported setup and security boundaries.
