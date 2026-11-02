import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { Conversation, Message, WorkspaceJob, WorkspaceJobDetail, WorkspaceQuestion, WorkspaceRepository, WorkspaceSnapshot, WorkspaceTurn } from './api/types'
import { WorkspaceApiError } from './api/types'

const mocks = vi.hoisted(() => ({
  resumeSession: vi.fn(),
  bootstrap: vi.fn(),
  listRepositories: vi.fn(),
  registerRepository: vi.fn(),
  snapshot: vi.fn(),
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  messages: vi.fn(),
  sendMessage: vi.fn(),
  answerQuestion: vi.fn(),
  jobDetail: vi.fn(),
  requestJobAction: vi.fn(),
  events: vi.fn(),
}))

vi.mock('./api/client', () => ({
  workspaceClient: mocks,
}))

const repository: WorkspaceRepository = {
  schema_version: 1,
  repository_id: 'repo-1',
  display_name: 'sample-app',
}

const conversation: Conversation = {
  schema_version: 1,
  conversation_id: 'conversation-1',
  repository_id: 'repo-1',
  title: 'New design chat',
  specification_refs: [],
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
  revision: 1,
}

const userMessage: Message = {
  schema_version: 1,
  message_id: 'message-1',
  conversation_id: 'conversation-1',
  role: 'user',
  content: 'Design a deployment dashboard.',
  created_at: '2026-11-01T00:00:00Z',
  sequence: 1,
}

function emptySnapshot(): WorkspaceSnapshot {
  return {
    schema_version: 1,
    repository_id: 'repo-1',
    conversations: [],
    jobs: [],
    active_turns: [],
    active_questions: [],
    event_cursor: 0,
  }
}

describe('workspace shell', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    localStorage.clear()
    mocks.resumeSession.mockRejectedValue(new Error('No session'))
    mocks.bootstrap.mockResolvedValue(undefined)
    mocks.listRepositories.mockResolvedValue([])
    mocks.registerRepository.mockResolvedValue(repository)
    mocks.snapshot.mockResolvedValue(emptySnapshot())
    mocks.listConversations.mockResolvedValue([])
    mocks.createConversation.mockResolvedValue(conversation)
    mocks.messages.mockResolvedValue([])
    mocks.sendMessage.mockResolvedValue(undefined)
    mocks.answerQuestion.mockResolvedValue(undefined)
    mocks.jobDetail.mockResolvedValue(undefined)
    mocks.requestJobAction.mockResolvedValue({
      action_id: 'action-1',
      job_id: 'job-1',
      action: 'cancel',
      state: 'accepted',
      job_revision: 2,
    })
    mocks.events.mockImplementation((_repositoryId: string, _after: number, signal: AbortSignal) =>
      (async function* () {
        await new Promise<void>((resolve) => {
          if (signal.aborted) return resolve()
          signal.addEventListener('abort', () => resolve(), { once: true })
        })
        yield* []
      })(),
    )
  })

  it('bootstraps the session and guides a developer through repository setup', async () => {
    const user = userEvent.setup()
    render(<App />)

    const tokenField = await screen.findByLabelText('Workspace launch token')
    await user.type(tokenField, 'a'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Open workspace' }))

    expect(await screen.findByText(/Start with the idea/)).toBeTruthy()
    expect(mocks.bootstrap).toHaveBeenCalledWith('a'.repeat(40))
    expect(screen.getByLabelText('Repository path')).toBeTruthy()
  })

  it('creates a chat, sends the first message, and keeps the selection in the URL', async () => {
    const user = userEvent.setup()
    mocks.resumeSession.mockResolvedValue(undefined)
    mocks.listRepositories.mockResolvedValue([repository])
    mocks.listConversations.mockImplementation(async () => {
      return mocks.createConversation.mock.calls.length ? [conversation] : []
    })
    mocks.messages.mockImplementation(async () => {
      return mocks.sendMessage.mock.calls.length ? [userMessage] : []
    })

    render(<App />)

    await screen.findByRole('button', { name: /New design chat/ })
    await user.click(screen.getByRole('button', { name: /New design chat/ }))
    expect(await screen.findByRole('heading', { name: 'What would you like to build?' })).toBeTruthy()
    expect(new URL(window.location.href).searchParams.get('conversation')).toBe('conversation-1')

    const composer = screen.getByRole('textbox', { name: 'Message Powdrr' })
    await user.type(composer, 'Design a deployment dashboard.')
    fireEvent.keyDown(composer, { key: 'Enter', shiftKey: false, isComposing: false })

    await waitFor(() => expect(mocks.sendMessage).toHaveBeenCalledWith('repo-1', 'conversation-1', 'Design a deployment dashboard.'))
    expect(await screen.findByText('Design a deployment dashboard.')).toBeTruthy()
    const transcript = screen.getByLabelText('Design conversation')
    expect(within(transcript).getByText('You')).toBeTruthy()
  })

  it('renders a persisted interview question and submits its schema-valid answer', async () => {
    const user = userEvent.setup()
    const question: WorkspaceQuestion = {
      schema_version: 1,
      question_id: 'question-1',
      conversation_id: 'conversation-1',
      turn_id: 'turn-1',
      prompt: 'What should search include?',
      response_schema: { type: 'string', minLength: 3 },
      run_id: 'run-1',
      attempt_id: 'attempt-1',
      state: 'open',
      answer: null,
      delivery_receipt_id: null,
      revision: 1,
      created_at: '2026-11-01T00:00:00Z',
      updated_at: '2026-11-01T00:00:00Z',
    }
    const turn: WorkspaceTurn = {
      schema_version: 1,
      turn_id: 'turn-1',
      conversation_id: 'conversation-1',
      user_message_id: 'message-1',
      run_id: 'run-1',
      attempt_id: 'attempt-1',
      state: 'waiting_for_input',
      final_message_id: null,
      error: null,
      revision: 3,
      created_at: '2026-11-01T00:00:00Z',
      updated_at: '2026-11-01T00:00:00Z',
    }
    let currentQuestion = question
    mocks.resumeSession.mockResolvedValue(undefined)
    mocks.listRepositories.mockResolvedValue([repository])
    mocks.listConversations.mockResolvedValue([conversation])
    mocks.snapshot.mockImplementation(async () => ({
      ...emptySnapshot(),
      active_turns: [turn],
      active_questions: [currentQuestion],
    }))
    mocks.messages.mockResolvedValue([userMessage])
    mocks.answerQuestion.mockImplementation(async (_repoId: string, _questionId: string, _revision: number, answer: unknown) => {
      currentQuestion = { ...question, state: 'answered', answer, revision: 2 }
      return currentQuestion
    })

    render(<App />)

    expect(await screen.findByText('What should search include?')).toBeTruthy()
    await user.type(screen.getByLabelText('Your answer'), 'Product names')
    await user.click(screen.getByRole('button', { name: /Send answer/ }))

    await waitFor(() => expect(mocks.answerQuestion).toHaveBeenCalledWith(
      'repo-1',
      'question-1',
      1,
      'Product names',
    ))
    const questionCard = within(await screen.findByRole('region', { name: 'Interview question' }))
    expect(questionCard.getByText('Answer received')).toBeTruthy()
    expect(questionCard.getByText('Product names')).toBeTruthy()
  })

  it('lists running and ready jobs and opens the selected job status', async () => {
    const user = userEvent.setup()
    const runningJob: WorkspaceJob = {
      schema_version: 1,
      job_id: 'job-running',
      repository_id: 'repo-1',
      kind: 'planning',
      title: 'Plan search',
      state: 'running',
      conversation_id: 'conversation-1',
      dependency_ids: ['job-complete'],
      artifact_refs: [],
      detail: 'Reviewing related modules.',
      revision: 2,
      created_at: '2026-11-01T00:00:00Z',
      updated_at: '2026-11-01T00:01:00Z',
    }
    const readyJob: WorkspaceJob = {
      ...runningJob,
      job_id: 'job-ready',
      kind: 'pr_execution',
      title: 'Create search PR',
      state: 'ready',
      detail: '',
    }
    const completedJob: WorkspaceJob = {
      ...readyJob,
      job_id: 'job-complete',
      title: 'Build search index',
      state: 'succeeded',
      detail: 'Created the search index.',
      updated_at: '2026-11-01T00:02:00Z',
    }
    const failedJob: WorkspaceJob = {
      ...readyJob,
      job_id: 'job-failed',
      title: 'Publish search changes',
      state: 'failed',
      detail: 'Validation failed.',
      updated_at: '2026-11-01T00:03:00Z',
    }
    const detail: WorkspaceJobDetail = {
      schema_version: 1,
      job: runningJob,
      latest_attempt: {
        schema_version: 1,
        attempt_id: 'attempt-1',
        run_id: 'run-1',
        source_spec_revision: 'spec-r3',
        created_at: '2026-11-01T00:00:00Z',
      },
      attempts: [{
        schema_version: 1,
        attempt_id: 'attempt-1',
        run_id: 'run-1',
        source_spec_revision: 'spec-r3',
        created_at: '2026-11-01T00:00:00Z',
      }],
      stage: 'Gathering repository context',
      blockers: [],
      question: null,
      outputs: ['artifacts/search-plan.md'],
      allowed_actions: ['cancel'],
    }
    mocks.resumeSession.mockResolvedValue(undefined)
    mocks.listRepositories.mockResolvedValue([repository])
    mocks.listConversations.mockResolvedValue([conversation])
    mocks.snapshot.mockResolvedValue({ ...emptySnapshot(), jobs: [runningJob, readyJob, completedJob, failedJob] })
    mocks.messages.mockResolvedValue([userMessage])
    mocks.jobDetail
      .mockResolvedValueOnce(detail)
      .mockResolvedValueOnce(detail)
      .mockResolvedValue({ ...detail, allowed_actions: [] })
    mocks.requestJobAction
      .mockRejectedValueOnce(new WorkspaceApiError('Stale job', 409, 'stale_revision'))
      .mockResolvedValueOnce({
        action_id: 'action-1',
        job_id: 'job-running',
        action: 'cancel',
        state: 'accepted',
        job_revision: 3,
      })

    const { container } = render(<App />)

    const app = within(container)
    expect(await app.findByRole('button', { name: /Plan search/ })).toBeTruthy()
    await user.click(app.getByRole('tab', { name: /Ready \/ pending/ }))
    const readyButton = await app.findByRole('button', { name: /Create search PR/ })
    expect(readyButton).toBeTruthy()
    await user.click(app.getByRole('tab', { name: /Running/ }))
    await user.click(app.getByRole('button', { name: /Plan search/ }))

    expect(await app.findByRole('heading', { name: 'Plan search' })).toBeTruthy()
    expect(app.getByText('Gathering repository context')).toBeTruthy()
    expect(app.getByText('run-1')).toBeTruthy()
    expect(app.getByRole('button', { name: 'Build search index' })).toBeTruthy()
    expect(new URL(window.location.href).searchParams.get('job')).toBe('job-running')
    expect(new URL(window.location.href).searchParams.has('conversation')).toBe(false)
    expect(mocks.jobDetail).toHaveBeenCalledWith('repo-1', 'job-running')

    await user.click(app.getByRole('button', { name: 'Request cancellation' }))
    expect((await app.findByRole('alert')).textContent).toMatch(/changed in another window/)
    expect(mocks.jobDetail).toHaveBeenCalledTimes(2)

    await user.click(app.getByRole('button', { name: 'Request cancellation' }))
    await waitFor(() => expect(mocks.requestJobAction).toHaveBeenCalledWith(
      'repo-1',
      'job-running',
      2,
      'cancel',
    ))
    expect((await app.findByRole('status')).textContent).toMatch(/Cancellation requested/)
    await waitFor(() => expect(app.queryByRole('button', { name: 'Request cancellation' })).toBeNull())

    await user.click(app.getByRole('tab', { name: /History/ }))
    const historyFilter = app.getByLabelText('Filter job history')
    const historyList = within(app.getByRole('navigation', { name: 'Job history' }))
    expect(historyList.getByRole('button', { name: /Build search index/ })).toBeTruthy()
    await user.selectOptions(historyFilter, 'failed')
    expect(historyList.getByRole('button', { name: /Publish search changes/ })).toBeTruthy()
    expect(historyList.queryByRole('button', { name: /Build search index/ })).toBeNull()
  })
})
