import type {
  ApiError,
  Conversation,
  ConversationMessages,
  Message,
  WorkspaceEvent,
  WorkspaceJobDetail,
  WorkspaceQuestion,
  WorkspaceRepository,
  WorkspaceSnapshot,
} from './types'
import { WorkspaceApiError } from './types'

const apiRoot = '/api/v1'

function requestId(): string {
  return crypto.randomUUID()
}

async function decodeError(response: Response): Promise<WorkspaceApiError> {
  const body = (await response.json().catch(() => null)) as
    | { error?: ApiError }
    | null
  return new WorkspaceApiError(
    body?.error?.message ?? `Workspace request failed (${response.status}).`,
    response.status,
    body?.error?.code,
    body?.error?.current_event_cursor,
  )
}

export class WorkspaceClient {
  private csrfToken: string | null = null
  private readonly pendingMessages = new Map<
    string,
    { requestId: string; messageId: string }
  >()
  private readonly pendingAnswers = new Map<string, string>()

  async bootstrap(token: string): Promise<void> {
    const response = await fetch(`${apiRoot}/session`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    })
    if (!response.ok) throw await decodeError(response)
    this.csrfToken = ((await response.json()) as { csrf_token: string }).csrf_token
  }

  async resumeSession(): Promise<void> {
    const response = await fetch(`${apiRoot}/session/refresh`, {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!response.ok) throw await decodeError(response)
    this.csrfToken = ((await response.json()) as { csrf_token: string }).csrf_token
  }

  clearSession(): void {
    this.csrfToken = null
  }

  async revokeSession(): Promise<void> {
    await this.mutate(`${apiRoot}/session/revoke`, { method: 'POST' })
    this.clearSession()
  }

  async listRepositories(): Promise<WorkspaceRepository[]> {
    const response = await this.read(`${apiRoot}/repositories`)
    return ((await response.json()) as { repositories: WorkspaceRepository[] }).repositories
  }

  async registerRepository(path: string): Promise<WorkspaceRepository> {
    const response = await this.mutate(`${apiRoot}/repositories`, {
      method: 'POST',
      body: JSON.stringify({ path, request_id: requestId() }),
    })
    return (await response.json()) as WorkspaceRepository
  }

  async snapshot(repositoryId: string): Promise<WorkspaceSnapshot> {
    const response = await this.read(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/snapshot`,
    )
    return (await response.json()) as WorkspaceSnapshot
  }

  async jobDetail(repositoryId: string, jobId: string): Promise<WorkspaceJobDetail> {
    const response = await this.read(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/jobs/${encodeURIComponent(jobId)}`,
    )
    return (await response.json()) as WorkspaceJobDetail
  }

  async listConversations(repositoryId: string): Promise<Conversation[]> {
    const response = await this.read(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/conversations`,
    )
    return ((await response.json()) as { conversations: Conversation[] }).conversations
  }

  async createConversation(repositoryId: string): Promise<Conversation> {
    const response = await this.mutate(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/conversations`,
      {
        method: 'POST',
        body: JSON.stringify({
          title: 'New design chat',
          request_id: requestId(),
        }),
      },
    )
    return (await response.json()) as Conversation
  }

  async messages(repositoryId: string, conversationId: string): Promise<Message[]> {
    const response = await this.read(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
    )
    return ((await response.json()) as ConversationMessages).messages
  }

  async sendMessage(
    repositoryId: string,
    conversationId: string,
    content: string,
  ): Promise<void> {
    const pendingKey = JSON.stringify([conversationId, content])
    const pending = this.pendingMessages.get(pendingKey) ?? {
      requestId: requestId(),
      messageId: requestId(),
    }
    this.pendingMessages.set(pendingKey, pending)
    await this.mutate(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify({
          request_id: pending.requestId,
          message_id: pending.messageId,
          content,
        }),
      },
    )
    this.pendingMessages.delete(pendingKey)
  }

  async answerQuestion(
    repositoryId: string,
    questionId: string,
    expectedRevision: number,
    answer: unknown,
  ): Promise<WorkspaceQuestion> {
    const payload = JSON.stringify(answer)
    const pendingKey = JSON.stringify([questionId, payload])
    const requestIdValue = this.pendingAnswers.get(pendingKey) ?? requestId()
    this.pendingAnswers.set(pendingKey, requestIdValue)
    const response = await this.mutate(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/questions/${encodeURIComponent(questionId)}/answer`,
      {
        method: 'POST',
        body: JSON.stringify({
          request_id: requestIdValue,
          expected_revision: expectedRevision,
          answer,
        }),
      },
    )
    this.pendingAnswers.delete(pendingKey)
    return (await response.json()) as WorkspaceQuestion
  }

  async *events(
    repositoryId: string,
    after: number,
    signal: AbortSignal,
  ): AsyncGenerator<WorkspaceEvent> {
    const response = await this.read(
      `${apiRoot}/repositories/${encodeURIComponent(repositoryId)}/events?after=${after}`,
      { signal },
    )
    if (!response.ok) throw await decodeError(response)
    if (!response.body) throw new Error('The workspace event stream is unavailable.')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let dataLines: string[] = []
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) return
        buffer += decoder.decode(value, { stream: true })
        let boundary = buffer.indexOf('\n')
        while (boundary !== -1) {
          const line = buffer.slice(0, boundary).replace(/\r$/, '')
          buffer = buffer.slice(boundary + 1)
          if (line === '') {
            if (dataLines.length) {
              yield JSON.parse(dataLines.join('\n')) as WorkspaceEvent
              dataLines = []
            }
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trimStart())
          }
          boundary = buffer.indexOf('\n')
        }
      }
    } finally {
      await reader.cancel().catch(() => undefined)
    }
  }

  private async read(path: string, init: RequestInit = {}): Promise<Response> {
    const response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
    })
    if (!response.ok) throw await decodeError(response)
    return response
  }

  private async mutate(path: string, init: RequestInit): Promise<Response> {
    if (!this.csrfToken) throw new WorkspaceApiError('Sign in to continue.', 401)
    const response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': this.csrfToken,
        ...init.headers,
      },
    })
    if (!response.ok) throw await decodeError(response)
    return response
  }
}

export const workspaceClient = new WorkspaceClient()
