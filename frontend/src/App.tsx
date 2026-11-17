import { NavLink, Route, Routes } from "react-router-dom";
import { Activity, ShieldCheck } from "lucide-react";

import { EvalDashboard } from "./pages/EvalDashboard";
import { KycPage } from "./pages/KycPage";
import { cn } from "./lib/utils";

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition",
          isActive ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground",
        )
      }
    >
      {children}
    </NavLink>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,_rgba(99,102,241,0.08),_transparent_55%)]">
      <header className="sticky top-0 z-20 border-b border-border/70 bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-3 px-4 py-2.5">
          <div className="flex items-center gap-2">
            <span className="rounded-lg bg-primary p-1.5 text-primary-foreground shadow-sm">
              <ShieldCheck className="h-4 w-4" />
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold">KYC Copilot</p>
              <p className="text-[10px] text-muted-foreground">guarded agent · live screen context · PII masking</p>
            </div>
          </div>
          <nav className="flex items-center gap-1">
            <NavItem to="/">
              <ShieldCheck className="h-3.5 w-3.5" /> Journey
            </NavItem>
            <NavItem to="/eval">
              <Activity className="h-3.5 w-3.5" /> Evaluations
            </NavItem>
          </nav>
        </div>
      </header>

      <Routes>
        <Route path="/" element={<KycPage />} />
        <Route path="/eval" element={<EvalDashboard />} />
        <Route path="*" element={<KycPage />} />
      </Routes>

      <footer className="mx-auto max-w-[1500px] px-4 pb-6 text-[10px] text-muted-foreground">
        Simulated PAN registry, OCR, address geocoder and bank mandate gateway. Set{" "}
        <code className="rounded bg-muted px-1">GEMINI_API_KEY</code> and{" "}
        <code className="rounded bg-muted px-1">DATABASE_URL</code> for live integrations.
      </footer>
    </div>
  );
}
