# Kharcha — web app

The Next.js frontend (plain JavaScript, App Router). How to run it with the
API is in the [main README](../README.md#5-run-the-web-app-phase-3-being-built).

```bash
npm ci          # install the exact versions in package-lock.json
npm run dev     # http://localhost:3000 (needs the API running, see .env.example)
npm run lint    # ESLint
npm test        # Vitest
npm run build   # production build
```

How the browser reaches the API and where the session lives:
[ADR 022](../docs/decisions/022-frontend-session-and-api-proxy.md).
