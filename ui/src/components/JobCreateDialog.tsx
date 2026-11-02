import { useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import type { Conversation, WorkspaceJob, WorkspaceJobCreateInput } from '../api/types'
import { WorkspaceApiError } from '../api/types'

type JobCreateDialogProps = {
  conversations: Conversation[]
  jobs: WorkspaceJob[]
  repositoryName: string
  onClose: () => void
  onCreate: (input: WorkspaceJobCreateInput) => Promise<void>
}

export function JobCreateDialog({ conversations, jobs, repositoryName, onClose, onCreate }: JobCreateDialogProps) {
  const [kind, setKind] = useState<WorkspaceJob['kind']>('planning')
  const [conversationId, setConversationId] = useState('')
  const [specRef, setSpecRef] = useState('')
  const [planJobId, setPlanJobId] = useState('')
  const [approvedChangeRef, setApprovedChangeRef] = useState('')
  const [providerRef, setProviderRef] = useState('provider:deepinfra-flash')
  const [title, setTitle] = useState('')
  const [confirmedApproval, setConfirmedApproval] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef<string | null>(null)

  const specConversations = useMemo(
    () => conversations.filter((conversation) => conversation.specification_refs.length > 0),
    [conversations],
  )
  const selectedConversation = specConversations.find((conversation) => conversation.conversation_id === conversationId)
  const selectedPlan = jobs.find((job) => job.job_id === planJobId)
  const completedPlans = jobs.filter((job) => job.kind === 'planning' && job.state === 'succeeded' && job.artifact_refs.length > 0)
  const sourceRevision = selectedConversation
    ? `conversation-revision:${selectedConversation.conversation_id}:${selectedConversation.revision}`
    : ''
  const sourceRef = kind === 'planning' ? specRef.trim() : approvedChangeRef.trim()
  const canSubmit = Boolean(
    title.trim() && providerRef.trim() && sourceRef &&
    (kind === 'planning' ? selectedConversation && sourceRevision : selectedPlan && confirmedApproval),
  )

  const changeKind = (nextKind: WorkspaceJob['kind']) => {
    setKind(nextKind)
    setError(null)
    setTitle('')
    setConfirmedApproval(false)
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit || submitting) return
    requestId.current ??= crypto.randomUUID()
    const input: WorkspaceJobCreateInput = {
      request_id: requestId.current,
      kind,
      title: title.trim(),
      conversation_id: kind === 'planning' ? selectedConversation?.conversation_id ?? null : selectedPlan?.conversation_id ?? null,
      dependency_ids: kind === 'pr_execution' && selectedPlan ? [selectedPlan.job_id] : [],
      job_inputs: kind === 'planning' ? { pinned_spec_ref: specRef } : { approved_change_ref: approvedChangeRef.trim() },
      source_refs: [sourceRef],
      source_spec_revision: kind === 'planning' ? sourceRevision : null,
      provider_config_refs: [providerRef.trim()],
    }
    setSubmitting(true)
    setError(null)
    try {
      await onCreate(input)
      requestId.current = null
      onClose()
    } catch (cause) {
      if (cause instanceof WorkspaceApiError) requestId.current = null
      setError(cause instanceof Error ? cause.message : 'The job could not be created.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="job-create-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !submitting) onClose()
    }}>
      <section className="job-create-dialog" role="dialog" aria-modal="true" aria-labelledby="job-create-title">
        <div className="job-create-dialog__heading">
          <div><span className="eyebrow">NEW AUTONOMOUS JOB</span><h2 id="job-create-title">Create a job</h2></div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose} disabled={submitting}>×</button>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <label>Job type
            <select value={kind} onChange={(event) => changeKind(event.target.value as WorkspaceJob['kind'])}>
              <option value="planning">Planning</option>
              <option value="pr_execution">PR execution</option>
            </select>
          </label>
          <label>Job title
            <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={512} required placeholder={kind === 'planning' ? 'Plan this feature' : 'Implement approved change'} />
          </label>

          {kind === 'planning' ? (
            <>
              <label>Design chat with a linked specification
                <select value={conversationId} onChange={(event) => {
                  setConversationId(event.target.value)
                  setSpecRef('')
                }} required>
                  <option value="">Choose a design chat</option>
                  {specConversations.map((conversation) => <option key={conversation.conversation_id} value={conversation.conversation_id}>{conversation.title}</option>)}
                </select>
              </label>
              {selectedConversation && <label>Specification reference
                <select value={specRef} onChange={(event) => setSpecRef(event.target.value)} required>
                  <option value="">Choose a linked specification</option>
                  {selectedConversation.specification_refs.map((ref) => <option key={ref} value={ref}>{ref}</option>)}
                </select>
              </label>}
              {specConversations.length === 0 && <p className="job-create-dialog__hint">Finish a design chat and link its validated specification before creating a planning job.</p>}
            </>
          ) : (
            <>
              <label>Completed planning job
                <select value={planJobId} onChange={(event) => {
                  setPlanJobId(event.target.value)
                  setApprovedChangeRef('')
                }} required>
                  <option value="">Choose a completed plan</option>
                  {completedPlans.map((job) => <option key={job.job_id} value={job.job_id}>{job.title}</option>)}
                </select>
              </label>
              <label>Proposed change from this plan
                <select value={approvedChangeRef} onChange={(event) => setApprovedChangeRef(event.target.value)} required>
                  <option value="">Choose a proposed change</option>
                  {(selectedPlan?.artifact_refs ?? []).map((ref) => <option key={ref} value={ref}>{ref}</option>)}
                </select>
              </label>
              <label className="job-create-dialog__check">
                <input type="checkbox" checked={confirmedApproval} onChange={(event) => setConfirmedApproval(event.target.checked)} />
                I confirm this proposed change has been approved for execution.
              </label>
              {completedPlans.length === 0 && <p className="job-create-dialog__hint">A completed planning job with proposed change references is required before PR execution can be created.</p>}
            </>
          )}

          <label>Provider configuration reference
            <input value={providerRef} onChange={(event) => setProviderRef(event.target.value)} required maxLength={256} />
            <small>Credentials stay in the local workspace configuration; enter a reference, never a secret.</small>
          </label>

          <section className="job-create-review" aria-label="Job review">
            <strong>Review</strong>
            <span>Repository: {repositoryName}</span>
            {kind === 'planning' && sourceRevision && <span>Pinned chat revision: {sourceRevision}</span>}
            {kind === 'pr_execution' && selectedPlan && <span>Depends on: {selectedPlan.title}</span>}
            <span>Input: {sourceRef || 'Choose the source reference above'}</span>
            <span>Provider: {providerRef || 'Enter a provider configuration reference'}</span>
          </section>
          {error && <p className="job-create-dialog__error" role="alert">{error}</p>}
          <div className="job-create-dialog__actions">
            <button type="button" className="secondary-button" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="primary-button" disabled={!canSubmit || submitting}>{submitting ? 'Creating…' : 'Create job'}<span aria-hidden="true">→</span></button>
          </div>
        </form>
      </section>
    </div>
  )
}
