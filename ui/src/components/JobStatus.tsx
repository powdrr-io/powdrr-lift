import type { Conversation, WorkspaceJob, WorkspaceJobAction, WorkspaceJobDetail } from '../api/types'
import { JobTimeline } from './JobTimeline'

const stateLabels: Record<WorkspaceJobDetail['job']['state'], string> = {
  pending: 'Pending',
  ready: 'Ready',
  running: 'Running',
  waiting_for_input: 'Waiting for input',
  waiting_for_external: 'Waiting for external work',
  interrupted: 'Interrupted',
  succeeded: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

function formatKind(kind: WorkspaceJobDetail['job']['kind']): string {
  return kind === 'planning' ? 'Planning' : 'PR execution'
}

function elapsedLabel(start: string, end: string): string {
  const totalMinutes = Math.floor(Math.max(0, new Date(end).getTime() - new Date(start).getTime()) / 60_000)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function timeSince(value: string): string {
  const minutes = Math.floor(Math.max(0, Date.now() - Date.parse(value)) / 60_000)
  if (minutes < 1) return 'Started just now'
  if (minutes < 60) return `Started ${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `Started ${hours}h ago`
  return `Started ${Math.floor(hours / 24)}d ago`
}

export function JobStatus({
  detail,
  pendingActions,
  actionMessage,
  onAction,
  dependencies,
  originatingConversation,
  onSelectJob,
  onSelectConversation,
}: {
  detail: WorkspaceJobDetail
  pendingActions: WorkspaceJobAction[]
  actionMessage: { kind: 'status' | 'error'; text: string } | null
  onAction: (action: WorkspaceJobAction) => void
  dependencies: WorkspaceJob[]
  originatingConversation: Conversation | null
  onSelectJob: (jobId: string) => void
  onSelectConversation: (conversationId: string) => void
}) {
  const { job } = detail
  const isActive = ['running', 'waiting_for_input', 'waiting_for_external'].includes(job.state)

  return (
    <section className="job-status" aria-label="Autonomous job status">
      <header className="job-status__header">
        <div>
          <span className="eyebrow">AUTONOMOUS WORK</span>
          <h1>{job.title}</h1>
          <span className="job-status__kind">{formatKind(job.kind)}</span>
        </div>
        <span className={`job-state job-state--${job.state.replaceAll('_', '-')}`}>
          <span aria-hidden="true" />{stateLabels[job.state]}
        </span>
      </header>

      <div className="job-status__body">
        {actionMessage && (
          <p className={`job-status__action-message job-status__action-message--${actionMessage.kind}`} role={actionMessage.kind === 'error' ? 'alert' : 'status'}>
            {actionMessage.text}
          </p>
        )}
        {detail.allowed_actions.length > 0 && (
          <section className="job-status__section" aria-label="Job actions">
            <h2>Actions</h2>
            <div className="job-status__actions">
              {detail.allowed_actions.map((action) => {
                const labels: Record<WorkspaceJobAction, string> = {
                  start: 'Start',
                  cancel: 'Request cancellation',
                  retry: 'Retry',
                  resume: 'Resume',
                }
                return (
                  <button
                    key={action}
                    type="button"
                    className={action === 'cancel' ? 'job-action job-action--secondary' : 'job-action'}
                    disabled={pendingActions.includes(action)}
                    onClick={() => onAction(action)}
                  >
                    {pendingActions.includes(action) ? 'Sending…' : labels[action]}
                  </button>
                )
              })}
            </div>
          </section>
        )}

        <section className="job-status__section">
          <h2>Run overview</h2>
          <dl className="job-status__metadata">
            <div><dt>Elapsed</dt><dd>{isActive ? timeSince(job.created_at) : elapsedLabel(job.created_at, job.updated_at)}</dd></div>
            <div><dt>Last activity</dt><dd>{new Date(job.updated_at).toLocaleString()}</dd></div>
            {job.conversation_id && (
              <div className="job-status__metadata-action">
                <dt>Design chat</dt>
                <dd>{originatingConversation
                  ? <button type="button" onClick={() => onSelectConversation(originatingConversation.conversation_id)}>{originatingConversation.title}</button>
                  : job.conversation_id}</dd>
              </div>
            )}
          </dl>
        </section>

        {detail.question && (
          <section className="job-status__section job-status__notice">
            <h2>{detail.question.state === 'open' ? 'Waiting on a question' : 'Latest question'}</h2>
            <p>{detail.question.prompt}</p>
            {detail.question.answer !== null && (
              <pre>{typeof detail.question.answer === 'string' ? detail.question.answer : JSON.stringify(detail.question.answer, null, 2)}</pre>
            )}
          </section>
        )}

        {detail.blockers.length > 0 && (
          <section className="job-status__section">
            <h2>Waiting for prerequisites</h2>
            <ul className="job-status__list">
              {detail.blockers.map((blocker) => (
                <li key={blocker.job_id}>
                  <span>{blocker.title}</span>
                  <span className={`job-state-label job-state-label--${blocker.state.replaceAll('_', '-')}`}>
                    {stateLabels[blocker.state]}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {dependencies.length > 0 && (
          <section className="job-status__section">
            <h2>Dependencies</h2>
            <ul className="job-status__dependencies">
              {dependencies.map((dependency) => (
                <li key={dependency.job_id}>
                  <button type="button" onClick={() => onSelectJob(dependency.job_id)}>{dependency.title}</button>
                  <span className={`job-state-label job-state-label--${dependency.state.replaceAll('_', '-')}`}>{stateLabels[dependency.state]}</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {(detail.attempts.length > 0 || detail.stage || job.detail) && <JobTimeline detail={detail} />}

        {detail.outputs.length > 0 && (
          <section className="job-status__section">
            <h2>Outputs</h2>
            <ul className="job-status__outputs">
              {detail.outputs.map((output) => <li key={output}><code>{output}</code></li>)}
            </ul>
          </section>
        )}

        {!job.detail && !detail.stage && !detail.question && detail.blockers.length === 0 && !detail.latest_attempt && detail.outputs.length === 0 && (
          <p className="job-status__empty">Status details will appear here as work progresses.</p>
        )}
      </div>
    </section>
  )
}
