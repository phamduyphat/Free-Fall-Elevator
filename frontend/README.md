# SREMS Frontend

The SREMS frontend is a React and TypeScript operations dashboard for the
Secure and Reliable Elevator Monitoring and Control System.

It currently provides the application shell and views for:

- fleet overview;
- elevator monitoring;
- credential management;
- security alerts;
- analytics; and
- audit logs.

The frontend is intentionally read-only until the corresponding authenticated
backend workflows and operational data sources are connected.

## Requirements

- Node.js 20 or newer
- npm
- The SREMS FastAPI backend running from the repository root

## Install dependencies

From this directory:

```powershell
npm install
```

## Development

Start the Vite development server:

```powershell
npm run dev
```

The UI is available at:

```text
http://127.0.0.1:5173
```

The Vite development server proxies `/api` requests to:

```text
http://127.0.0.1:8000
```

Start the backend separately from the repository root:

```powershell
python main.py
```

The frontend currently consumes:

```text
GET /api/v1/fleet/status
```

The backend returns explicit `UNKNOWN` elevator states until a live
operational telemetry source is connected. The UI does not fabricate current
floor, availability, alert, credential, analytics, or audit data.

## Available scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Start the Vite development server |
| `npm run build` | Run TypeScript checks and create a production build |
| `npm run preview` | Preview the production build locally |

## Project structure

```text
frontend/
├── index.html              Application HTML entry point
├── package.json            Dependencies and npm scripts
├── vite.config.ts          Vite configuration and API proxy
├── tsconfig*.json          TypeScript project configuration
└── src/
    ├── App.tsx             Navigation and dashboard views
    ├── main.tsx            React application entry point
    ├── styles.css          Shared responsive styles
    └── vite-env.d.ts       Vite type declarations
```

## UI navigation

The application uses in-memory view navigation rather than a URL router at
this stage. The available sections are:

- **Overview:** fleet summary, KPI placeholders, and recent activity state.
- **Elevators:** individual elevator cards and service matrix.
- **Credentials:** cardholder and credential-management workspace.
- **Security alerts:** alert queue and severity summaries.
- **Analytics:** time-window and traffic-profile controls for Mode 5 KPIs.
- **Audit log:** ledger search and integrity-verification workspace.

Screens show loading, empty, unavailable, and error states instead of
silently displaying success-shaped placeholder data.

## Production build

Create the production assets with:

```powershell
npm run build
```

The generated files are written to `frontend/dist/`. The directory is
ignored by Git and should be served by a production web server or deployed
through the chosen hosting platform.

For production deployment, configure the web server to forward `/api` to the
FastAPI service and serve the frontend application entry point for client-side
navigation.

## Development conventions

- Keep API response types explicit in TypeScript.
- Keep authorization enforcement in the backend; UI visibility is not a
  security boundary.
- Do not add operational fixture data to represent live elevator state.
- Preserve the backend's read-only analytics boundary described in
  `../analytics/design_decisions_analytics.md`.
- Run `npm run build` before submitting frontend changes.
