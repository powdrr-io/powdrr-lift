type ArtifactKind = 'implementation-pr' | 'alteration-pr' | 'issue' | 'pull-request' | 'artifact'

type ArtifactLink = {
  kind: ArtifactKind
  label: string
  reference: string
  href: string | null
}

const labels: Record<ArtifactKind, string> = {
  'implementation-pr': 'Implementation PR',
  'alteration-pr': 'Alteration PR',
  issue: 'GitHub issue',
  'pull-request': 'Pull request · purpose not specified',
  artifact: 'Artifact',
}

function safeGitHubUrl(value: string): string | null {
  try {
    const url = new URL(value)
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password) return null
    if (!/^\/[\w.-]+\/[\w.-]+\/(pull|issues)\/\d+\/?$/.test(url.pathname)) return null
    return url.toString()
  } catch {
    return null
  }
}

function parseArtifact(reference: string): ArtifactLink {
  const taggedKind = reference.startsWith('implementation-pr:')
    ? 'implementation-pr'
    : reference.startsWith('alteration-pr:')
      ? 'alteration-pr'
      : null
  if (taggedKind) {
    const rawUrl = reference.slice(taggedKind.length + 1)
    return {
      kind: taggedKind,
      label: labels[taggedKind],
      reference,
      href: safeGitHubUrl(rawUrl),
    }
  }

  const href = safeGitHubUrl(reference)
  if (href?.includes('/issues/')) return { kind: 'issue', label: labels.issue, reference, href }
  if (href?.includes('/pull/')) return { kind: 'pull-request', label: labels['pull-request'], reference, href }
  return { kind: 'artifact', label: labels.artifact, reference, href: null }
}

export function ArtifactLinks({ outputs }: { outputs: string[] }) {
  if (outputs.length === 0) return null
  const artifacts = outputs.map(parseArtifact)
  const includesPullRequest = artifacts.some((artifact) =>
    artifact.kind === 'implementation-pr' || artifact.kind === 'alteration-pr' || artifact.kind === 'pull-request',
  )

  return (
    <section className="job-status__section" aria-labelledby="job-outputs-title">
      <h2 id="job-outputs-title">Outputs</h2>
      <ul className="job-status__outputs">
        {artifacts.map((artifact, index) => (
          <li key={`${index}-${artifact.reference}`}>
            <span className="job-output__kind">{artifact.label}</span>
            {artifact.href
              ? <a href={artifact.href} target="_blank" rel="noopener noreferrer">{artifact.href}</a>
              : <code>{artifact.reference}</code>}
          </li>
        ))}
      </ul>
      {includesPullRequest && <p className="job-output__status-note">Review and merge status is not included with these references.</p>}
    </section>
  )
}
