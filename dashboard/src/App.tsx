import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { FleetPage } from "./pages/FleetPage";
import { DevicePage } from "./pages/DevicePage";
import { AnalyticsPage } from "./pages/AnalyticsPage";

export function App() {
  return (
    <BrowserRouter>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <span className="brand-mark">◉</span>
            <div>
              <strong>HomeLinkIoT</strong>
              <span className="brand-sub">connected appliance control</span>
            </div>
          </div>
          <nav>
            <NavLink to="/devices" className={({ isActive }) => (isActive ? "active" : "")}>
              Fleet
            </NavLink>
            <NavLink to="/analytics" className={({ isActive }) => (isActive ? "active" : "")}>
              Analytics
            </NavLink>
          </nav>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<Navigate to="/devices" replace />} />
            <Route path="/devices" element={<FleetPage />} />
            <Route path="/devices/:id" element={<DevicePage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="*" element={<p className="muted">Page not found.</p>} />
          </Routes>
        </main>
        <footer>
          Local MQTT 3.1.1 · AWS-IoT-compatible shadow · Lambda-shaped API · see{" "}
          <code>docs/AWS_MAPPING.md</code>
        </footer>
      </div>
    </BrowserRouter>
  );
}
