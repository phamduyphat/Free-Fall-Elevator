# SREMS Frontend

The SREMS frontend is a React and TypeScript operations dashboard for the
Secure and Reliable Elevator Monitoring and Control System.

It currently provides the application shell and views for:

- fleet overview;
- simulation and 30-floor visual shaft;
- wear & tear balancing and 6-month benchmark visualization;
- elevator monitoring and service matrix;
- credential management;
- security alerts and incident handling;
- analytics; and
- audit logs with integrity verification.

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
cd ..
python main.py
```

*(Optional) If using a virtual environment:*

```powershell
cd ..
.\.venv\Scripts\Activate.ps1
python main.py
```

The frontend connects to the SREMS API endpoints (`/api/v1/*`) including
fleet status, hall and car calls, emergency incidents, credentials, and audit logs.

## Running the simulation

1. Start the backend (`python main.py` in the root repository).
2. Start the frontend (`npm run dev` in `frontend/`).
3. Open `http://localhost:5173` and click **"Simulator (stub)"** in the navigation sidebar:
   - **Shaft panel:** Displays real-time animated car positions for `E1`–`E5` across B1 and Floors 1–30.
   - **Car Operating Panel (COP):** Select a car, swipe a card (e.g. `ADMIN-CARD-0001`), press destination buttons, and test `HELP` or `MEDICAL` emergency actions.
   - **Hall panel:** Select an origin floor to submit Up/Down hall calls or request `E5` Freight service.
   - **Security & diagnostics:** Open the **"Incidents & diagnostics"** tab to resolve alarms, send rescue cars, and confirm medical boarding.

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

- **Overview:** fleet summary, KPI indicators, and recent operational activity.
- **Simulation:** interactive 30-floor visual shaft, car operating panel, and hall panel.
- **Elevators:** individual elevator cards and service matrix.
- **Credentials:** cardholder registration and credential lifecycle management.
- **Security alerts:** incident queue, rescue car dispatch, and diagnostics.
- **Analytics:** real-time operational indicators and persisted Mode 5 database reports.
- **Audit log:** database-backed audit chain search and SHA-256 integrity verification.

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
