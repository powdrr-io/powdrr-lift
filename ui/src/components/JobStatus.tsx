import type { WorkspaceJobDetail } from '../api/types'

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

export function JobStatus({ detail }: { detail: WorkspaceJobDetail }) {
  const { job } = detail

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
        {job.detail && (
          <section className="job-status__section">
            <h2>Latest update</h2>
            <p className="job-status__detail">{job.detail}</p>
          </section>
        )}

        {detail.stage && (
          <section className="job-status__section">
            <h2>Current stage</h2>
            <p className="job-status__stage"><span className="pulse-dot" />{detail.stage}</p>
          </section>
        )}

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

        {detail.latest_attempt && (
          <section className="job-status__section">
            <h2>Latest attempt</h2>
            <dl className="job-status__metadata">
              <div><dt>Run</dt><dd>{detail.latest_attempt.run_id}</dd></div>
              <div><dt>Attempt</dt><dd>{detail.latest_attempt.attempt_id}</dd></div>
              {detail.latest_attempt.source_spec_revision && (
                <div><dt>Specification</dt><dd>{detail.latest_attempt.source_spec_revision}</dd></div>
              )}
              {detail.attempts.length > 1 && (
                <div><dt>Attempts</dt><dd>{detail.attempts.length}</dd></div>
              )}
            </dl>
          </section>
        )}

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
