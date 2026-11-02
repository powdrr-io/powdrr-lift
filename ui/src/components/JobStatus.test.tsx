import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WorkspaceJobDetail } from '../api/types'
import { JobStatus } from './JobStatus'

const interruptedDetail: WorkspaceJobDetail = {
  schema_version: 1,
  job: {
    schema_version: 1,
    job_id: 'job-interrupted',
    repository_id: 'repo-1',
    kind: 'planning',
    title: 'Plan search',
    state: 'interrupted',
    conversation_id: 'conversation-1',
    dependency_ids: [],
    artifact_refs: ['artifacts/search-plan.md'],
    detail: 'Worker exited unexpectedly.',
    revision: 4,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:05:00Z',
  },
  latest_attempt: {
    schema_version: 1,
    attempt_id: 'attempt-2',
    run_id: 'run-2',
    source_spec_revision: 'revision-7',
    created_at: '2026-11-01T00:03:00Z',
  },
  attempts: [{
    schema_version: 1,
    attempt_id: 'attempt-2',
    run_id: 'run-2',
    source_spec_revision: 'revision-7',
    created_at: '2026-11-01T00:03:00Z',
  }],
  stage: null,
  blockers: [],
  question: null,
  outputs: ['artifacts/search-plan.md'],
  allowed_actions: ['retry', 'cancel'],
}

describe('JobStatus recovery review', () => {
  afterEach(() => cleanup())

  it('shows available context and requires confirmation before requesting retry', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    render(
      <JobStatus
        detail={interruptedDetail}
        pendingActions={[]}
        actionMessage={null}
        onAction={onAction}
        dependencies={[]}
        originatingConversation={null}
        onSelectJob={vi.fn()}
        onSelectConversation={vi.fn()}
      />,
    )

    expect(screen.getByText('Checkpoint availability')).toBeTruthy()
    expect(screen.getByText('Not reported by the workspace API')).toBeTruthy()
    expect(screen.getByText('run-2')).toBeTruthy()
    expect(screen.getByText('revision-7')).toBeTruthy()
    expect(screen.getByText('artifacts/search-plan.md')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Review retry' }))
    expect(screen.getByRole('region', { name: 'Confirm retry' })).toBeTruthy()
    expect(onAction).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Confirm retry' }))
    expect(onAction).toHaveBeenCalledOnce()
    expect(onAction).toHaveBeenCalledWith('retry')
  })
})
