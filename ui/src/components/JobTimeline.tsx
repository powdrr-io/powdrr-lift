import type { WorkspaceJobDetail } from '../api/types'

const stateLabels: Record<WorkspaceJobDetail['job']['state'], string> = {
  pending: 'Pending',
  ready: 'Ready',
  running: 'Running',
  waiting_for_input: 'Needs input',
  waiting_for_external: 'Waiting for external work',
  interrupted: 'Interrupted',
  succeeded: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

function Attempt({ detail, index, current, currentState }: {
  detail: WorkspaceJobDetail['attempts'][number]
  index: number
  current: boolean
  currentState: WorkspaceJobDetail['job']['state']
}) {
  const isActive = ['running', 'waiting_for_input', 'waiting_for_external'].includes(currentState)
  return (
    <li className={`job-timeline__item${current ? ' job-timeline__item--current' : ''}`}>
      <span className="job-timeline__marker" aria-hidden="true" />
      <div className="job-timeline__content">
        <div className="job-timeline__heading">
          <strong>{current && isActive ? `Attempt ${index + 1} · Active` : `Attempt ${index + 1}`}</strong>
          <time dateTime={detail.created_at}>{new Date(detail.created_at).toLocaleString()}</time>
        </div>
        <dl className="job-timeline__metadata">
          <div><dt>Run</dt><dd>{detail.run_id}</dd></div>
          <div><dt>Attempt ID</dt><dd>{detail.attempt_id}</dd></div>
          {detail.source_spec_revision && <div><dt>Source revision</dt><dd>{detail.source_spec_revision}</dd></div>}
        </dl>
      </div>
    </li>
  )
}

export function JobTimeline({ detail }: { detail: WorkspaceJobDetail }) {
  const attempts = [...detail.attempts].sort((left, right) => left.created_at.localeCompare(right.created_at))
  const previousAttempts = attempts.slice(0, -1)
  const latestAttempt = attempts.at(-1)
  const activity = detail.stage ?? detail.job.detail ?? stateLabels[detail.job.state]

  return (
    <section className="job-status__section job-timeline" aria-labelledby="job-timeline-title">
      <h2 id="job-timeline-title">Timeline</h2>
      <ol className="job-timeline__list">
        {!latestAttempt && (
          <li className="job-timeline__item">
            <span className="job-timeline__marker" aria-hidden="true" />
            <div className="job-timeline__content">
              <div className="job-timeline__heading">
                <strong>Job created</strong>
                <time dateTime={detail.job.created_at}>{new Date(detail.job.created_at).toLocaleString()}</time>
              </div>
            </div>
          </li>
        )}
        {latestAttempt && <Attempt detail={latestAttempt} index={attempts.length - 1} current currentState={detail.job.state} />}
        <li className="job-timeline__item job-timeline__item--activity">
          <span className="job-timeline__marker" aria-hidden="true" />
          <div className="job-timeline__content">
            <div className="job-timeline__heading">
              <strong>Latest activity</strong>
              <time dateTime={detail.job.updated_at}>{new Date(detail.job.updated_at).toLocaleString()}</time>
            </div>
            <p>{activity}</p>
          </div>
        </li>
      </ol>
      {previousAttempts.length > 0 && (
        <details className="job-timeline__previous">
          <summary>Previous attempts ({previousAttempts.length})</summary>
          <ol className="job-timeline__list">
            {previousAttempts.map((attempt, index) => <Attempt key={attempt.attempt_id} detail={attempt} index={index} current={false} currentState={detail.job.state} />)}
          </ol>
        </details>
      )}
    </section>
  )
}
