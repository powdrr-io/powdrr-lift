import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ArtifactLinks } from './ArtifactLinks'

describe('ArtifactLinks', () => {
  afterEach(() => cleanup())

  it('labels implementation and alteration pull requests separately', () => {
    render(<ArtifactLinks outputs={[
      'implementation-pr:https://github.com/acme/sample/pull/77',
      'alteration-pr:https://github.com/acme/sample/pull/76',
    ]} />)

    const implementation = screen.getByRole('link', { name: 'https://github.com/acme/sample/pull/77' })
    const alteration = screen.getByRole('link', { name: 'https://github.com/acme/sample/pull/76' })
    expect(implementation.closest('li')?.textContent).toContain('Implementation PR')
    expect(alteration.closest('li')?.textContent).toContain('Alteration PR')
    expect(implementation.getAttribute('target')).toBe('_blank')
    expect(implementation.getAttribute('rel')).toContain('noopener')
    expect(screen.getByText('Review and merge status is not included with these references.')).toBeTruthy()
  })

  it('does not infer purpose from untyped PR references or link unsafe destinations', () => {
    render(<ArtifactLinks outputs={[
      'https://github.com/acme/sample/pull/75',
      'javascript:alert(1)',
      'https://attacker@github.com/acme/sample/pull/74',
      'https://github.com:444/acme/sample/pull/73',
      'artifact:plan.md',
    ]} />)

    expect(screen.getByRole('link', { name: 'https://github.com/acme/sample/pull/75' }).closest('li')?.textContent)
      .toContain('purpose not specified')
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByText('javascript:alert(1)')).toBeTruthy()
    expect(screen.getByText('artifact:plan.md')).toBeTruthy()
  })

  it('links GitHub issue references with an issue label', () => {
    render(<ArtifactLinks outputs={['https://github.com/acme/sample/issues/12']} />)

    const issue = screen.getByRole('link', { name: 'https://github.com/acme/sample/issues/12' })
    expect(issue.closest('li')?.textContent).toContain('GitHub issue')
  })
})
