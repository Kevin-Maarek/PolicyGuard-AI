import { Bot, UserRound } from "lucide-react";
import type { ReactNode } from "react";

export function ChatTurn({ role = "assistant", label, children }: {
  role?: "assistant" | "user";
  label?: string;
  children: ReactNode;
}) {
  return <article className={`investigation-turn ${role}`}>
    <span className="investigation-avatar" aria-hidden="true">
      {role === "user" ? <UserRound size={17} /> : <Bot size={19} />}
    </span>
    <div className="investigation-message">
      <span className="investigation-speaker">{label || (role === "user" ? "You" : "PolicyGuard")}</span>
      <div className="investigation-bubble">{children}</div>
    </div>
  </article>;
}
