import { isEngramTool } from "./metrics"

export type SessionKind = "primary" | "child" | "unknown"

export interface SessionLookup {
  session: {
    get(input: { path: { id: string } }): Promise<{
      data?: { parentID?: string | null }
    }>
  }
}

export class SubagentGate {
  private readonly classifications = new Map<string, Promise<SessionKind>>()
  private readonly unlocked = new Set<string>()

  constructor(private readonly client: SessionLookup) {}

  async classify(sessionID: string): Promise<SessionKind> {
    const cached = this.classifications.get(sessionID)
    if (cached) return cached

    const classification = this.lookup(sessionID)
    this.classifications.set(sessionID, classification)
    return classification
  }

  async shouldBlock(sessionID: string, tool: string): Promise<boolean> {
    if (isEngramTool(tool) || this.unlocked.has(sessionID)) return false
    return (await this.classify(sessionID)) !== "primary"
  }

  markSuccessfulEngramCall(sessionID: string, tool: string): void {
    if (isEngramTool(tool)) this.unlocked.add(sessionID)
  }

  clear(sessionID: string): void {
    this.classifications.delete(sessionID)
    this.unlocked.delete(sessionID)
  }

  clearAll(): void {
    this.classifications.clear()
    this.unlocked.clear()
  }

  private async lookup(sessionID: string): Promise<SessionKind> {
    try {
      const result = await this.client.session.get({ path: { id: sessionID } })
      if (!result.data) return "unknown"
      return result.data.parentID ? "child" : "primary"
    } catch {
      // An unresolvable session must not bypass the child-session gate.
      return "unknown"
    }
  }
}
