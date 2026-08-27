import type { ReactNode } from "react";

interface PageLayoutProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  sidebar?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function PageLayout({ title, description, actions, sidebar, children, className = "" }: PageLayoutProps) {
  return (
    <section className={`page-layout ${className}`.trim()}>
      <header className="page-header">
        <div>
          <h1>{title}</h1>
          {description ? <div className="page-description">{description}</div> : null}
        </div>
        {actions ? <div className="page-actions">{actions}</div> : null}
      </header>
      <div className={sidebar ? "page-with-sidebar" : "page-content"}>
        {sidebar ? <aside className="page-sidebar">{sidebar}</aside> : null}
        <main className="page-content">{children}</main>
      </div>
    </section>
  );
}
