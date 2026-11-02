/** Browser-facing records from the versioned powdrr_contracts workspace API. */
export type WorkspaceRepository = {
  schema_version: 1
  repository_id: string
  display_name: string
}

export type Conversation = {
  schema_version: 1
  conversation_id: string
  repository_id: string
  title: string
  specification_refs: string[]
  created_at: string
  updated_at: string
  revision: number
}

export type Message = {
  schema_version: 1
  message_id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  sequence: number
}

export type WorkspaceTurn = {
  schema_version: 1
  turn_id: string
  conversation_id: string
  user_message_id: string
  run_id: string | null
  attempt_id: string | null
  state: 'accepted' | 'running' | 'waiting_for_input' | 'succeeded' | 'failed' | 'cancelled'
  final_message_id: string | null
  error: string | null
  revision: number
  created_at: string
  updated_at: string
}

export type WorkspaceQuestion = {
  schema_version: 1
  question_id: string
  conversation_id: string
  turn_id: string
  prompt: string
  response_schema: Record<string, unknown>
  run_id: string
  attempt_id: string
  state: 'open' | 'answered' | 'delivered' | 'cancelled'
  answer: unknown | null
  delivery_receipt_id: string | null
  revision: number
  created_at: string
  updated_at: string
}

export type WorkspaceJob = {
  schema_version: 1
  job_id: string
  repository_id: string
  kind: 'planning' | 'pr_execution'
  title: string
  state: 'pending' | 'ready' | 'running' | 'waiting_for_input' | 'waiting_for_external' | 'interrupted' | 'succeeded' | 'failed' | 'cancelled'
  conversation_id: string | null
  dependency_ids: string[]
  artifact_refs: string[]
  detail: string
  revision: number
  created_at: string
  updated_at: string
}

export type WorkspaceJobAttempt = {
  schema_version: 1
  attempt_id: string
  run_id: string
  source_spec_revision: string | null
  created_at: string
}

export type WorkspaceJobBlocker = {
  schema_version: 1
  job_id: string
  title: string
  state: WorkspaceJob['state']
}

export type WorkspaceJobDetail = {
  schema_version: 1
  job: WorkspaceJob
  latest_attempt: WorkspaceJobAttempt | null
  attempts: WorkspaceJobAttempt[]
  stage: string | null
  blockers: WorkspaceJobBlocker[]
  question: WorkspaceQuestion | null
  outputs: string[]
  allowed_actions: WorkspaceJobAction[]
}

export type WorkspaceJobAction = 'start' | 'cancel' | 'retry' | 'resume'

export type WorkspaceJobActionResult = {
  action_id: string
  job_id: string
  action: WorkspaceJobAction
  state: 'accepted'
  job_revision: number
}

export type WorkspaceSnapshot = {
  schema_version: 1
  repository_id: string
  conversations: Conversation[]
  jobs: WorkspaceJob[]
  active_turns: WorkspaceTurn[]
  active_questions: WorkspaceQuestion[]
  event_cursor: number
}

export type WorkspaceEvent = {
  schema_version: number
  sequence: number
  repository_id: string
  entity_type: string
  entity_id: string
  event_type: string
  payload: Record<string, unknown>
  created_at: string
  attempt_id?: string | null
  revision?: number | null
}

export type ConversationMessages = {
  messages: Message[]
  next_before_sequence: number | null
}

export type ApiError = {
  code: string
  message: string
  current_revision?: number
  current_event_cursor?: number
}

export class WorkspaceApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly currentEventCursor?: number

  constructor(
    message: string,
    status: number,
    code?: string,
    currentEventCursor?: number,
  ) {
    super(message)
    this.name = 'WorkspaceApiError'
    this.status = status
    this.code = code
    this.currentEventCursor = currentEventCursor
  }
}
