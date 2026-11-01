import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { workspaceClient } from './api/client'
import type { Conversation, Message, WorkspaceRepository, WorkspaceSnapshot } from './api/types'
import { WorkspaceApiError } from './api/types'
import './App.css'

type LoadState = 'checking' | 'signed-out' | 'loading' | 'ready'
type Theme = 'light' | 'dark' | 'system'

function readSelection(): { repositoryId: string | null; conversationId: string | null } {
  const params = new URLSearchParams(window.location.search)
  return {
    repositoryId: params.get('repository'),
    conversationId: params.get('conversation'),
  }
}

function updateUrl(repositoryId: string | null, conversationId: string | null): void {
  const url = new URL(window.location.href)
  if (repositoryId) url.searchParams.set('repository', repositoryId)
  else url.searchParams.delete('repository')
  if (conversationId) url.searchParams.set('conversation', conversationId)
  else url.searchParams.delete('conversation')
  window.history.pushState({}, '', url)
}

function readDrafts(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem('powdrr-chat-drafts') ?? '{}') as Record<string, string>
  } catch {
    return {}
  }
}

function relativeTime(value: string): string {
  const elapsed = Math.max(0, Date.now() - new Date(value).getTime())
  const minutes = Math.floor(elapsed / 60_000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

function ConversationItem({
  conversation,
  active,
  running,
  onClick,
}: {
  conversation: Conversation
  active: boolean
  running: boolean
  onClick: () => void
}) {
  return (
    <button
      className={`conversation-row${active ? ' is-active' : ''}`}
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      title={conversation.title}
    >
      <span className="conversation-row__glyph" aria-hidden="true">◌</span>
      <span className="conversation-row__text">
        <span className="conversation-row__title">{conversation.title}</span>
        <span className="conversation-row__meta">
          {running && <span className="status-dot" aria-label="Interview in progress" />}
          {relativeTime(conversation.updated_at)}
        </span>
      </span>
    </button>
  )
}

function App() {
  const [loadState, setLoadState] = useState<LoadState>('checking')
  const [repositories, setRepositories] = useState<WorkspaceRepository[]>([])
  const [repositoryId, setRepositoryId] = useState<string | null>(readSelection().repositoryId)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(readSelection().conversationId)
  const [messages, setMessages] = useState<Message[]>([])
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null)
  const [token, setToken] = useState('')
  const [repoPath, setRepoPath] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>(readDrafts)
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem('powdrr-theme')
    return saved === 'dark' || saved === 'light' ? saved : 'system'
  })
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [connection, setConnection] = useState<'connected' | 'reconnecting'>('connected')
  const transcriptRef = useRef<HTMLDivElement>(null)
  const followTranscriptRef = useRef(true)
  const conversationRef = useRef<string | null>(conversationId)
  const eventCursorRef = useRef(0)

  const draft = conversationId ? drafts[conversationId] ?? '' : ''

  const selectedConversation = conversations.find((item) => item.conversation_id === conversationId) ?? null
  const activeTurn = snapshot?.active_turns.find((item) => item.conversation_id === conversationId)
  const filteredConversations = useMemo(() => {
    const query = search.trim().toLocaleLowerCase()
    if (!query) return conversations
    return conversations.filter((item) => item.title.toLocaleLowerCase().includes(query))
  }, [conversations, search])

  const refreshWorkspace = useCallback(async (selectedRepository: string) => {
    const [nextSnapshot, nextConversations] = await Promise.all([
      workspaceClient.snapshot(selectedRepository),
      workspaceClient.listConversations(selectedRepository),
    ])
    setSnapshot(nextSnapshot)
    eventCursorRef.current = nextSnapshot.event_cursor
    setConversations(nextConversations)
    const currentId = conversationRef.current
    const nextId = nextConversations.some((item) => item.conversation_id === currentId)
      ? currentId
      : nextConversations[0]?.conversation_id ?? null
    if (nextId !== currentId) {
      conversationRef.current = nextId
      setConversationId(nextId)
      updateUrl(selectedRepository, nextId)
    }
    if (nextId) {
      setMessages(await workspaceClient.messages(selectedRepository, nextId))
    } else {
      setMessages([])
    }
    return nextSnapshot
  }, [])

  const loadRepositories = useCallback(async () => {
    const nextRepositories = await workspaceClient.listRepositories()
    setRepositories(nextRepositories)
    const savedRepositoryId = readSelection().repositoryId
    const nextRepositoryId = nextRepositories.some((item) => item.repository_id === savedRepositoryId)
      ? savedRepositoryId
      : nextRepositories[0]?.repository_id ?? null
    setRepositoryId(nextRepositoryId)
    if (nextRepositoryId) {
      await refreshWorkspace(nextRepositoryId)
      updateUrl(nextRepositoryId, conversationRef.current)
    } else {
      setSnapshot(null)
      eventCursorRef.current = 0
      setConversations([])
      setMessages([])
      updateUrl(null, null)
    }
    setLoadState('ready')
  }, [refreshWorkspace])

  useEffect(() => {
    let current = true
    void workspaceClient.resumeSession().then(
      async () => {
        if (!current) return
        setLoadState('loading')
        try {
          await loadRepositories()
        } catch (cause) {
          if (!current) return
          if (cause instanceof WorkspaceApiError && cause.status === 401) {
            workspaceClient.clearSession()
            setLoadState('signed-out')
          } else {
            setError(cause instanceof Error ? cause.message : 'Workspace could not be loaded.')
            setLoadState('ready')
          }
        }
      },
      () => {
        if (current) setLoadState('signed-out')
      },
    )
    return () => { current = false }
  }, [loadRepositories])

  useEffect(() => {
    conversationRef.current = conversationId
  }, [conversationId])

  useEffect(() => {
    localStorage.setItem('powdrr-chat-drafts', JSON.stringify(drafts))
  }, [drafts])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('powdrr-theme', theme)
  }, [theme])

  useEffect(() => {
    if (!repositoryId || loadState !== 'ready') return
    const controller = new AbortController()
    let cursor = eventCursorRef.current
    let backoff = 500
    const connect = async () => {
      while (!controller.signal.aborted) {
        try {
          for await (const event of workspaceClient.events(repositoryId, cursor, controller.signal)) {
            if (event.sequence <= cursor) continue
            cursor = event.sequence
            eventCursorRef.current = cursor
            setConnection('connected')
            await refreshWorkspace(repositoryId)
            backoff = 500
          }
        } catch (cause) {
          if (controller.signal.aborted) return
          setConnection('reconnecting')
          if (cause instanceof WorkspaceApiError && cause.code === 'resync_required') {
            cursor = (await refreshWorkspace(repositoryId)).event_cursor
            continue
          }
          if (cause instanceof WorkspaceApiError && cause.status === 401) {
            workspaceClient.clearSession()
            setLoadState('signed-out')
            return
          }
          await new Promise((resolve) => window.setTimeout(resolve, backoff))
          backoff = Math.min(backoff * 2, 15_000)
          try {
            cursor = (await refreshWorkspace(repositoryId)).event_cursor
          } catch {
            // The next iteration retries from the last confirmed event cursor.
          }
        }
      }
    }
    void connect()
    return () => controller.abort()
  }, [loadState, repositoryId, refreshWorkspace])

  useEffect(() => {
    const handlePopState = () => {
      const selected = readSelection()
      conversationRef.current = selected.conversationId
      setConversationId(selected.conversationId)
      if (selected.repositoryId && selected.repositoryId !== repositoryId) {
        setRepositoryId(selected.repositoryId)
        void refreshWorkspace(selected.repositoryId).catch((cause: unknown) => {
          setError(cause instanceof Error ? cause.message : 'Repository could not be loaded.')
        })
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [refreshWorkspace, repositoryId])

  useEffect(() => {
    if (!repositoryId || !conversationId || loadState !== 'ready') return
    let current = true
    void workspaceClient.messages(repositoryId, conversationId).then(
      (nextMessages) => { if (current) setMessages(nextMessages) },
      (cause: unknown) => { if (current) setError(cause instanceof Error ? cause.message : 'Messages could not be loaded.') },
    )
    return () => { current = false }
  }, [conversationId, loadState, repositoryId])

  useEffect(() => {
    if (followTranscriptRef.current && transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight
    }
  }, [messages])

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await workspaceClient.bootstrap(token.trim())
      setToken('')
      setLoadState('loading')
      await loadRepositories()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Sign in failed.')
      setLoadState('signed-out')
    } finally {
      setBusy(false)
    }
  }

  const registerRepository = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!repoPath.trim()) return
    setBusy(true)
    setError(null)
    try {
      const repository = await workspaceClient.registerRepository(repoPath.trim())
      const nextRepositories = await workspaceClient.listRepositories()
      setRepositories(nextRepositories)
      setRepositoryId(repository.repository_id)
      setConversationId(null)
      conversationRef.current = null
      setRepoPath('')
      await refreshWorkspace(repository.repository_id)
      updateUrl(repository.repository_id, conversationRef.current)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Repository could not be registered.')
    } finally {
      setBusy(false)
    }
  }

  const createConversation = async () => {
    if (!repositoryId) return
    setBusy(true)
    setError(null)
    try {
      const conversation = await workspaceClient.createConversation(repositoryId)
      conversationRef.current = conversation.conversation_id
      setConversationId(conversation.conversation_id)
      setMessages([])
      await refreshWorkspace(repositoryId)
      updateUrl(repositoryId, conversation.conversation_id)
      setDrafts((previous) => ({ ...previous, [conversation.conversation_id]: '' }))
      setMobileSidebarOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Chat could not be created.')
    } finally {
      setBusy(false)
    }
  }

  const selectConversation = (id: string) => {
    conversationRef.current = id
    setConversationId(id)
    setDrafts((previous) => ({ ...previous, [id]: previous[id] ?? '' }))
    setMobileSidebarOpen(false)
    if (repositoryId) updateUrl(repositoryId, id)
  }

  const sendMessage = async () => {
    const content = draft.trim()
    if (!repositoryId || !conversationId || !content || busy || activeTurn) return
    setBusy(true)
    setError(null)
    followTranscriptRef.current = true
    try {
      await workspaceClient.sendMessage(repositoryId, conversationId, content)
      setDrafts((previous) => ({ ...previous, [conversationId]: '' }))
      await refreshWorkspace(repositoryId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Message could not be sent.')
    } finally {
      setBusy(false)
    }
  }

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void sendMessage()
    }
  }

  const selectRepository = async (id: string) => {
    setRepositoryId(id)
    conversationRef.current = null
    setConversationId(null)
    updateUrl(id, null)
    setMobileSidebarOpen(false)
    try {
      await refreshWorkspace(id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Repository could not be loaded.')
    }
  }

  if (loadState === 'checking' || loadState === 'loading') {
    return <div className="loading-screen"><span className="brand-mark">p</span><span>Opening workspace…</span></div>
  }

  if (loadState === 'signed-out') {
    return (
      <main className="auth-screen">
        <div className="auth-card">
          <div className="brand-lockup"><span className="brand-mark">p</span><span>powdrr</span></div>
          <p className="eyebrow">LOCAL WORKSPACE</p>
          <h1>Pick up where good ideas begin.</h1>
          <p className="auth-copy">Connect to your private workspace with its one-time launch token.</p>
          <form onSubmit={signIn} className="auth-form">
            <label htmlFor="bootstrap-token">Workspace launch token</label>
            <input id="bootstrap-token" autoComplete="off" type="password" value={token} onChange={(event) => setToken(event.target.value)} required minLength={32} />
            <button className="primary-button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Open workspace'}<span aria-hidden="true">↗</span></button>
          </form>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <p className="auth-footnote">Your conversations and workspace data stay on this device.</p>
        </div>
      </main>
    )
  }

  const hasRepository = Boolean(repositoryId)
  const currentConversationId = conversationId ?? ''
  const currentRepository = repositories.find((item) => item.repository_id === repositoryId)

  return (
    <div className={`workspace${mobileSidebarOpen ? ' sidebar-open' : ''}`}>
      {mobileSidebarOpen && <button className="mobile-backdrop" aria-label="Close navigation" onClick={() => setMobileSidebarOpen(false)} />}
      <aside className="sidebar" aria-label="Workspace navigation">
        <div className="sidebar-topline">
          <a className="brand-lockup" href="#" onClick={(event) => event.preventDefault()} aria-label="Powdrr home">
            <span className="brand-mark">p</span><span>powdrr</span>
          </a>
          <button className="icon-button sidebar-collapse" aria-label="Close navigation" onClick={() => setMobileSidebarOpen(false)}>×</button>
        </div>

        {hasRepository ? (
          <>
            <label className="repository-select-label" htmlFor="repository-select">WORKSPACE</label>
            <select id="repository-select" className="repository-select" value={repositoryId ?? ''} onChange={(event) => void selectRepository(event.target.value)}>
              {repositories.map((repository) => <option key={repository.repository_id} value={repository.repository_id}>{repository.display_name}</option>)}
            </select>
            <button className="new-chat-button" type="button" onClick={() => void createConversation()} disabled={busy}>
              <span className="new-chat-icon" aria-hidden="true">＋</span><span>New design chat</span>
            </button>
            <div className="chat-search">
              <span aria-hidden="true">⌕</span>
              <input aria-label="Search chats" placeholder="Search chats" value={search} onChange={(event) => setSearch(event.target.value)} />
              {search && <button className="clear-search" aria-label="Clear search" onClick={() => setSearch('')}>×</button>}
            </div>
            <div className="sidebar-section-heading"><span>DESIGN CHATS</span><span>{filteredConversations.length}</span></div>
            <nav className="conversation-list" aria-label="Design chats">
              {filteredConversations.map((conversation) => (
                <ConversationItem
                  key={conversation.conversation_id}
                  conversation={conversation}
                  active={conversation.conversation_id === conversationId}
                  running={snapshot?.active_turns.some((turn) => turn.conversation_id === conversation.conversation_id) ?? false}
                  onClick={() => selectConversation(conversation.conversation_id)}
                />
              ))}
              {filteredConversations.length === 0 && <p className="sidebar-empty">{search ? 'No chats match your search.' : 'Your design chats will appear here.'}</p>}
            </nav>
            <div className="jobs-placeholder">
              <div className="jobs-placeholder__heading"><span>JOBS</span><span className="coming-soon">NEXT</span></div>
              <div className="jobs-placeholder__content">
                <span className="jobs-placeholder__icon" aria-hidden="true">⌁</span>
                <p>Planning and PR work<br />will show up here.</p>
              </div>
            </div>
            <div className="sidebar-footer">
              <div className="repo-footer">
                <span className="repo-avatar" aria-hidden="true">{currentRepository?.display_name.slice(0, 1).toUpperCase() ?? 'W'}</span>
                <span className="repo-footer__text"><strong>{currentRepository?.display_name ?? 'Workspace'}</strong><small>Local workspace</small></span>
                <button className="icon-button theme-button" aria-label={`Theme: ${theme}`} title={`Theme: ${theme}`} onClick={() => setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system')}>{theme === 'dark' ? '☾' : theme === 'light' ? '☼' : '◐'}</button>
              </div>
            </div>
          </>
        ) : (
          <div className="sidebar-setup"><span className="eyebrow">GET STARTED</span><p>Register a Git repository to start a design chat.</p></div>
        )}
      </aside>

      <main className="main-pane">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileSidebarOpen(true)}>☰</button>
          <div className="breadcrumb"><span>{currentRepository?.display_name ?? 'Workspace'}</span><span className="breadcrumb-divider">/</span><span>{selectedConversation?.title ?? 'Design workspace'}</span></div>
          <div className="topbar-right">
            <span className={`connection-state ${connection}`}><span className="connection-dot" />{connection === 'connected' ? 'Connected' : 'Reconnecting'}</span>
            <button className="icon-button topbar-theme" aria-label={`Theme: ${theme}`} onClick={() => setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system')}>{theme === 'dark' ? '☾' : theme === 'light' ? '☼' : '◐'}</button>
          </div>
        </header>

        {!hasRepository ? (
          <section className="setup-view">
            <div className="setup-copy">
              <span className="eyebrow">YOUR LOCAL DESIGN STUDIO</span>
              <h1>Start with the idea.<br /><em>Leave with a clear spec.</em></h1>
              <p>Powdrr interviews you about quality, delivery order, and the details needed to build your feature. Your project stays on your machine.</p>
              <div className="setup-steps"><span><b>01</b> Choose a repository</span><span><b>02</b> Shape the feature</span><span><b>03</b> Plan and ship</span></div>
            </div>
            <form className="repo-card" onSubmit={registerRepository}>
              <span className="repo-card-icon" aria-hidden="true">⌘</span>
              <h2>Connect a project</h2>
              <p>Choose an existing Git repository on this computer.</p>
              <label htmlFor="repo-path">Repository path</label>
              <div className="path-field"><span aria-hidden="true">⌂</span><input id="repo-path" value={repoPath} onChange={(event) => setRepoPath(event.target.value)} placeholder="/Users/you/code/project" required /></div>
              <button className="primary-button" type="submit" disabled={busy || !repoPath.trim()}>{busy ? 'Connecting…' : 'Register repository'}<span aria-hidden="true">→</span></button>
              <small>Only the selected repository is available to this workspace.</small>
            </form>
          </section>
        ) : selectedConversation ? (
          <section className="conversation-view" aria-label="Design conversation">
            <div className="conversation-heading">
              <div><span className="eyebrow">FEATURE SPECIFICATION</span><h1>{selectedConversation.title}</h1></div>
              {activeTurn && <span className="working-pill"><span className="pulse-dot" />{activeTurn.state === 'waiting_for_input' ? 'Needs your input' : 'Thinking'}</span>}
            </div>
            <div
              className="transcript"
              ref={transcriptRef}
              onScroll={(event) => {
                const element = event.currentTarget
                followTranscriptRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 96
              }}
              aria-live="polite"
            >
              {messages.length === 0 ? (
                <div className="conversation-welcome">
                  <span className="welcome-orbit" aria-hidden="true">✳</span>
                  <h2>What would you like to build?</h2>
                  <p>Describe the feature in your own words. We’ll work through the details together.</p>
                  <div className="prompt-suggestions"><button onClick={() => setDrafts((previous) => ({ ...previous, [currentConversationId]: 'I want to add a feature that…' }))}>“I want to add a feature that…” <span>↗</span></button><button onClick={() => setDrafts((previous) => ({ ...previous, [currentConversationId]: 'Help me improve the onboarding flow for…' }))}>“Help me improve the onboarding…” <span>↗</span></button></div>
                </div>
              ) : messages.filter((message) => message.role !== 'system').map((message) => (
                <article className={`message message--${message.role}`} key={message.message_id}>
                    <div className={`message-avatar message-avatar--${message.role}`} aria-hidden="true">{message.role === 'user' ? 'Y' : <span className="brand-mark brand-mark--small">p</span>}</div>
                  <div className="message-body">
                    <div className="message-meta"><strong>{message.role === 'user' ? 'You' : 'Powdrr'}</strong><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time></div>
                    <div className="markdown-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>
                  </div>
                </article>
              ))}
              {activeTurn && <div className="assistant-pending"><span className="typing-indicator"><i /><i /><i /></span><span>{activeTurn.state === 'waiting_for_input' ? 'Waiting for your answer' : 'Working on your spec'}</span></div>}
            </div>
            {error && <div className="conversation-error" role="alert"><span>{error}</span><button onClick={() => setError(null)} aria-label="Dismiss error">×</button></div>}
            <div className="composer-wrap">
              <div className="composer">
                <textarea
                  aria-label="Message Powdrr"
                  placeholder={activeTurn ? 'The interview is continuing…' : 'Describe what you want to build'}
                  value={draft}
                  onChange={(event) => setDrafts((previous) => ({ ...previous, [currentConversationId]: event.target.value }))}
                  onKeyDown={handleComposerKeyDown}
                  rows={1}
                  disabled={Boolean(activeTurn)}
                />
                <button className="send-button" aria-label="Send message" onClick={() => void sendMessage()} disabled={busy || Boolean(activeTurn) || !draft.trim()}><span aria-hidden="true">↑</span></button>
              </div>
              <div className="composer-footnote"><span>Enter to send <span className="footnote-separator">·</span> Shift + Enter for a new line</span><span>AI can make mistakes. Review important details.</span></div>
            </div>
          </section>
        ) : (
          <section className="empty-chat-view">
            <div className="empty-chat-mark"><span className="brand-mark">p</span></div>
            <h1>Your next feature starts here.</h1>
            <p>Create a design chat to shape a complete, buildable specification.</p>
            <button className="primary-button" onClick={() => void createConversation()} disabled={busy}>Start a design chat <span aria-hidden="true">→</span></button>
          </section>
        )}
        <footer className="app-footer"><span>BUILT FOR THOUGHTFUL SOFTWARE</span><span>Local by design <i>✳</i></span></footer>
      </main>
    </div>
  )
}

export default App
