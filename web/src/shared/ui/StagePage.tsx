import type { ReactNode } from "react";

import { Icon } from "@/shared/ui/Icon";

export interface StageItem {
  title: string;
  text: string;
  status: string;
}

export function StagePage({
  stage,
  title,
  description,
  items,
  children,
}: {
  stage: string;
  title: string;
  description: string;
  items: StageItem[];
  children?: ReactNode;
}) {
  return (
    <div className="page">
      <div className="page-inner">
        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>{title}</h2>
              <p>{description}</p>
            </div>
            <span className="chip accent">{stage}</span>
          </div>
          <div className="grid-3">
            {items.map((item) => (
              <div className="panel" key={item.title}>
                <div className="search-meta">
                  <Icon name="spark" size={14} /> {item.status}
                </div>
                <div style={{ fontSize: 14, fontWeight: 600, margin: "8px 0 5px" }}>
                  {item.title}
                </div>
                <div className="search-meta" style={{ lineHeight: 1.65 }}>
                  {item.text}
                </div>
              </div>
            ))}
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
