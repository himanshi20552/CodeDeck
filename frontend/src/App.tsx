import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  askRepositoryQuestion, cancelStaging, cleanupDeployment, configureAutoscaling, disableAutoscaling, getAutoscaling, clearProjectWorkspace, deleteProject, createProject,
  deployProduction, deployStaging, getAnalysis, getEvaluation,
  getFiles, getHealth, getMonitoringDashboard, getPipeline, getProject, getReleaseWorkflow, getSystemStatus,
  listDeployments, listProjects, runAiTests,
  startAnalysis,
  type Analysis, type Deployment, type Evaluation, type GeneratedFile,
  type GeneratedTestRun, type PipelineStep, type Project,
  type RepositoryAnswer, type ReleaseWorkflow, type MonitoringDashboard, type SystemStatus, type AutoscalingConfiguration, type AutoscalingStatus,
} from './api'

// ─── helpers ───────────────────────────────────────────────────────────────

type Page = 'overview' | 'release' | 'monitoring' | 'repository' | 'assistant' | 'intelligence' | 'testing' | 'docker' | 'kubernetes' | 'logs' | 'settings'

const PIPELINE_STEPS = [
  'Repository Analysis', 'Repository Intelligence', 'Tests',
  'Security', 'Docker', 'Kubernetes', 'Staging',
]

function statusColor(s: string) {
  if (s === 'completed') return 'text-emerald-400'
  if (s === 'failed') return 'text-red-400'
  if (s === 'running') return 'text-yellow-400'
  if (s === 'warning') return 'text-orange-400'
  return 'text-zinc-500'
}
function statusBg(s: string) {
  if (s === 'completed') return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
  if (s === 'failed') return 'bg-red-500/10 text-red-400 border-red-500/20'
  if (s === 'running') return 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
  if (s === 'warning') return 'bg-orange-500/10 text-orange-400 border-orange-500/20'
  return 'bg-zinc-800 text-zinc-500 border-zinc-700'
}
function stepIcon(s: string) {
  if (s === 'completed') return '✓'
  if (s === 'failed') return '✗'
  if (s === 'running') return '●'
  if (s === 'warning') return '⚠'
  return '○'
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return 'Not recorded'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return minutes < 60 ? `${minutes}m ${remainder}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function formatWorkflowTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : 'Not recorded'
}

function formatBytes(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return 'Not available'
  if (value < 1024) return `${Math.round(value)} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let amount = value / 1024
  let index = 0
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++ }
  return `${amount.toFixed(1)} ${units[index]}`
}

function workflowStatusClass(status: string) {
  switch (status) {
    case 'Completed': return 'border-emerald-700/50 bg-emerald-950/20 text-emerald-300'
    case 'Running': return 'border-sky-700/50 bg-sky-950/20 text-sky-300'
    case 'Queued': return 'border-amber-700/50 bg-amber-950/20 text-amber-300'
    case 'Failed': return 'border-red-700/50 bg-red-950/20 text-red-300'
    case 'Blocked': return 'border-orange-800/50 bg-orange-950/20 text-orange-300'
    case 'Skipped': return 'border-zinc-700 bg-zinc-900 text-zinc-400'
    default: return 'border-zinc-800 bg-zinc-900/50 text-zinc-500'
  }
}

function friendlyError(message: string) {
  const normalized = message.replace(/\s+/g, ' ').trim()
  const lower = normalized.toLowerCase()
  if (lower.includes('docker build failed')) return 'The container could not be built. Review the deployment analysis for the dependency or build configuration that needs attention.'
  if (lower.includes('cluster not reachable') || lower.includes('kind create cluster')) return 'The local Kubernetes cluster is unavailable. Start the kind cluster, then retry staging.'
  if (lower.includes('crashloopbackoff')) return 'The application container starts but exits repeatedly. Review the deployment analysis for its detected entrypoint or configuration issue.'
  if (lower.includes('progress deadline') || lower.includes('rollout failed')) return 'Kubernetes did not report the service as ready in time. Review the deployment analysis for readiness, database, or startup configuration fixes.'
  if (lower.includes('module not found') || lower.includes('no module named')) return 'A required runtime dependency or application module is missing. Review the deployment analysis for the suggested project fix.'
  if (lower.includes('timed out')) return 'The operation took longer than expected. Check the deployment analysis and retry after the affected service is available.'
  return normalized.split(/(?<=[.!?])\s+/)[0].slice(0, 240) || 'The operation could not be completed. Review the deployment analysis and try again.'
}

// ─── sub-components ─────────────────────────────────────────────────────────

function Pill({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="rounded border border-zinc-700 bg-zinc-800/60 px-3 py-2">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">{label}</div>
      <div className="mt-0.5 truncate font-mono text-sm text-zinc-200">{value ?? '—'}</div>
    </div>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-zinc-500">
      <span className="h-px flex-1 bg-zinc-800" />
      {children}
      <span className="h-px flex-1 bg-zinc-800" />
    </h2>
  )
}

function TerminalBlock({ text, maxH = '14rem' }: { text: string; maxH?: string }) {
  const ref = useRef<HTMLPreElement>(null)
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [text])
  return (
    <pre
      ref={ref}
      style={{ maxHeight: maxH }}
      className="overflow-auto rounded border border-zinc-700 bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-300"
    >
      {text || <span className="text-zinc-600">No output</span>}
    </pre>
  )
}

function renderInlineFormatting(str: string): React.ReactNode {
  const parts = str.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i} className="font-semibold text-zinc-100">{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`')) {
      return (
        <code key={i} className="rounded bg-zinc-800/80 px-1 py-0.5 font-mono text-[11px] text-sky-300 border border-zinc-700/50">
          {part.slice(1, -1)}
        </code>
      )
    }
    return part
  })
}

function FormattedText({ text, className = '' }: { text?: string | null; className?: string }) {
  if (!text) return null
  const lines = text.split('\n')
  return (
    <div className={`space-y-1.5 text-xs leading-relaxed ${className}`}>
      {lines.map((line, idx) => {
        const trimmed = line.trim()
        if (!trimmed) return <div key={idx} className="h-0.5" />
        if (trimmed.startsWith('### ')) {
          return (
            <h4 key={idx} className="text-[11px] font-semibold uppercase tracking-wider text-sky-400 mt-2">
              {trimmed.replace(/^###\s+/, '')}
            </h4>
          )
        }
        if (trimmed.startsWith('## ')) {
          return (
            <h3 key={idx} className="text-xs font-bold uppercase tracking-wider text-sky-300 mt-2.5 border-b border-zinc-800/80 pb-1">
              {trimmed.replace(/^##\s+/, '')}
            </h3>
          )
        }
        if (trimmed.startsWith('# ')) {
          return (
            <h2 key={idx} className="text-sm font-bold text-zinc-100 mt-2.5">
              {trimmed.replace(/^#\s+/, '')}
            </h2>
          )
        }
        if (trimmed.startsWith('* ') || trimmed.startsWith('- ')) {
          return (
            <div key={idx} className="flex gap-2 items-start ml-1 text-zinc-300">
              <span className="text-sky-400 font-bold shrink-0">•</span>
              <span>{renderInlineFormatting(trimmed.replace(/^[*-]\s+/, ''))}</span>
            </div>
          )
        }
        if (/^\d+\.\s+/.test(trimmed)) {
          const num = trimmed.match(/^(\d+)\.\s+/)?.[1]
          const content = trimmed.replace(/^\d+\.\s+/, '')
          return (
            <div key={idx} className="flex gap-2 items-start ml-1 text-zinc-300">
              <span className="text-amber-400 font-mono font-semibold shrink-0">{num}.</span>
              <span>{renderInlineFormatting(content)}</span>
            </div>
          )
        }
        return (
          <p key={idx} className="text-zinc-200">
            {renderInlineFormatting(line)}
          </p>
        )
      })}
    </div>
  )
}

// ─── main App ───────────────────────────────────────────────────────────────

export default function App() {
  const [page, setPage] = useState<Page>('overview')
  const [url, setUrl] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<Project | null>(null)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [pipeline, setPipeline] = useState<PipelineStep[]>([])
  const [releaseWorkflow, setReleaseWorkflow] = useState<ReleaseWorkflow | null>(null)
  const [monitoringData, setMonitoringData] = useState<MonitoringDashboard | null>(null)
  const [monitoringProjectFilter, setMonitoringProjectFilter] = useState('all')
  const [monitoringDeploymentFilter, setMonitoringDeploymentFilter] = useState('all')
  const [monitoringTimeFilter, setMonitoringTimeFilter] = useState('24h')
  const [monitoringError, setMonitoringError] = useState<string | null>(null)
  const [monitoringLoading, setMonitoringLoading] = useState(false)
  const [files, setFiles] = useState<GeneratedFile[]>([])
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [autoscalingStatus, setAutoscalingStatus] = useState<AutoscalingStatus | null>(null)
  const [autoscalingConfig, setAutoscalingConfig] = useState<AutoscalingConfiguration>({ min_replicas: 1, max_replicas: 3, target_cpu_utilization: 70, target_memory_utilization: null, behavior: { scale_up_stabilization_seconds: 0, scale_down_stabilization_seconds: 300 } })
  const [autoscalingBusy, setAutoscalingBusy] = useState(false)
  const [autoscalingError, setAutoscalingError] = useState<string | null>(null)
  const [qaResult, setQaResult] = useState<RepositoryAnswer | null>(null)
  const [question, setQuestion] = useState('What framework does this project use?')
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null)
  const [aiTests, setAiTests] = useState<GeneratedTestRun | null>(null)
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null)
  const [deploymentTarget, setDeploymentTarget] = useState('remote')
  const [apiOk, setApiOk] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [deploying, setDeploying] = useState(false)
  const [cancelingStaging, setCancelingStaging] = useState(false)
  const [deployingProd, setDeployingProd] = useState(false)
  const [askingQuestion, setAskingQuestion] = useState(false)
  const [runningAiTests, setRunningAiTests] = useState(false)
  const [loadingEval, setLoadingEval] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [logs, setLogs] = useState<Array<{ tag: string; text: string; ts: string }>>([])
  const selectedProjectId = useRef<number | null>(null)
  const pollStartedAt = useRef<Record<number, number>>({})
  const autoscalingFormInitialized = useRef<Record<number, boolean>>({})
  const autoscalingDeploymentId = deployments.find(d => ['remote_staging', 'staging', 'production_green'].includes(d.deployment_type) && ['SUCCEEDED', 'healthy', 'staging_healthy', 'production_healthy', 'awaiting_approval'].includes(d.status))?.id ?? null

  useEffect(() => {
    if (!selected || autoscalingDeploymentId == null) { setAutoscalingStatus(null); return }
    let stopped = false
    const load = () => getAutoscaling(selected.id, autoscalingDeploymentId).then(data => {
      if (stopped) return
      setAutoscalingStatus(data)
      setAutoscalingError(null)
      const config = data.configuration?.config
      if (config && !autoscalingFormInitialized.current[autoscalingDeploymentId]) {
        setAutoscalingConfig(config)
        autoscalingFormInitialized.current[autoscalingDeploymentId] = true
      }
    }).catch(err => { if (!stopped) setAutoscalingError(err instanceof Error ? err.message : 'Autoscaling status unavailable') })
    void load()
    const timer = window.setInterval(() => void load(), 10000)
    return () => { stopped = true; window.clearInterval(timer) }
  }, [selected?.id, autoscalingDeploymentId])

  const log = (tag: string, text: string) =>
    setLogs(p => [...p.slice(-500), { tag, text, ts: new Date().toLocaleTimeString() }])

  // On mount
  useEffect(() => {
    getHealth().then(() => setApiOk(true)).catch(() => setApiOk(false))
    listProjects().then(setProjects).catch(() => setProjects([]))
    getSystemStatus().then(setSystemStatus).catch(() => setSystemStatus(null))
  }, [])

  useEffect(() => {
    if (systemStatus) setDeploymentTarget(systemStatus.deployment_target)
  }, [systemStatus?.deployment_target])

  useEffect(() => {
    const timer = window.setInterval(() => {
      getSystemStatus().then(setSystemStatus).catch(() => setSystemStatus(null))
    }, 15000)
    return () => window.clearInterval(timer)
  }, [])

  // Poll serially while a server-side operation is active. Stop after 30 minutes.
  useEffect(() => {
    if (!selected) return
    const isBusy = selected.status === 'deploying' || selected.status === 'staging_cancel_requested' || selected.status === 'deploying_green' || selected.status === 'analyzing'
    if (!isBusy) {
      delete pollStartedAt.current[selected.id]
      return
    }
    pollStartedAt.current[selected.id] ??= Date.now()
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (stopped) return
      if (Date.now() - pollStartedAt.current[selected.id] > 30 * 60 * 1000) {
        setError('This operation is still running after 30 minutes. Polling stopped; refresh the project to check its latest state.')
        return
      }
      try {
        await loadProject(selected)
      } catch (err) {
        setError(`Could not refresh operation status: ${String((err as Error).message || err)}`)
      }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    timer = setTimeout(poll, 3000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [selected?.id, selected?.status])

  // Sync sub-results from analysis_result
  useEffect(() => {
    const extra = (analysis?.analysis_result ?? {}) as Record<string, unknown>
    setEvaluation((extra.evaluation_result as Evaluation | undefined) ?? null)
    setAiTests((extra.ai_test_result as GeneratedTestRun | undefined) ?? null)
    setQaResult(null)
  }, [selected?.id, analysis?.created_at])

  async function loadProject(project: Project, isManual = false) {
    if (isManual) setError(null)
    selectedProjectId.current = project.id
    setSelected(project)
    const [a, steps, fresh, generated, deps, workflow] = await Promise.all([
      getAnalysis(project.id),
      getPipeline(project.id),
      getProject(project.id),
      getFiles(project.id),
      listDeployments(project.id),
      getReleaseWorkflow(project.id),
    ])
    if (selectedProjectId.current !== project.id) return
    setAnalysis(a)
    setPipeline(steps)
    setSelected(fresh)
    setFiles(generated)
    setSelectedFile(prev => {
      if (!prev) return generated[0]?.filename ?? null
      return generated.some(f => f.filename === prev) ? prev : generated[0]?.filename ?? null
    })
    setDeployments(deps)
    setReleaseWorkflow(workflow)
    setProjects(prev => prev.map(p => p.id === fresh.id ? fresh : p))
  }

  function monitoringSince() {
    if (monitoringTimeFilter === 'all') return null
    const hours = monitoringTimeFilter === '1h' ? 1 : monitoringTimeFilter === '24h' ? 24 : 24 * 7
    return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString()
  }

  async function loadMonitoring() {
    setMonitoringLoading(true)
    try {
      const result = await getMonitoringDashboard({
        project_id: monitoringProjectFilter === 'all' ? null : Number(monitoringProjectFilter),
        deployment_id: monitoringDeploymentFilter === 'all' ? null : Number(monitoringDeploymentFilter),
        since: monitoringSince(), limit: 200,
      })
      setMonitoringData(result)
      setMonitoringError(null)
    } catch (err) {
      setMonitoringError(err instanceof Error ? err.message : 'Monitoring data is unavailable')
    } finally {
      setMonitoringLoading(false)
    }
  }

  useEffect(() => {
    if (page !== 'monitoring') return
    let closed = false
    let timer: ReturnType<typeof setInterval>
    const refresh = async () => {
      if (closed) return
      await loadMonitoring()
    }
    void refresh()
    timer = setInterval(refresh, 15000)
    return () => { closed = true; clearInterval(timer) }
  }, [page, monitoringProjectFilter, monitoringDeploymentFilter, monitoringTimeFilter])

  async function onManualReanalysis() {
    const target = monitoringProjectFilter !== 'all'
      ? projects.find(project => project.id === Number(monitoringProjectFilter))
      : selected
    if (!target) { setMonitoringError('Choose a project before starting analysis.'); return }
    setBusy(true)
    setMonitoringError(null)
    try {
      await startAnalysis(target.id)
      await loadProject(target)
      await loadMonitoring()
    } catch (err) {
      setMonitoringError(err instanceof Error ? err.message : 'Analysis could not be started')
    } finally {
      setBusy(false)
    }
  }

  async function onAnalyze(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    log('Analysis', `Starting analysis for ${url}`)
    try {
      const project = await createProject(url.trim())
      selectedProjectId.current = project.id
      setSelected(project)
      setProjects(prev => [project, ...prev.filter(p => p.id !== project.id)])
      log('Analysis', 'Repository cloned — running pipeline…')
      const result = await startAnalysis(project.id)
      setAnalysis(result)
      const [steps, fresh, generated, deps, workflow] = await Promise.all([
        getPipeline(project.id), getProject(project.id),
        getFiles(project.id), listDeployments(project.id), getReleaseWorkflow(project.id),
      ])
      setPipeline(steps)
      setSelected(fresh)
      setFiles(generated)
      setSelectedFile(generated[0]?.filename ?? null)
      setDeployments(deps)
      setReleaseWorkflow(workflow)
      setProjects(prev => prev.map(p => p.id === fresh.id ? fresh : p))
      log('Analysis', `Done — ${result.language} / ${result.framework}`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Analysis failed'
      setError(msg)
      log('Analysis', `ERROR: ${msg}`)
      if (selected) getPipeline(selected.id).then(setPipeline).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  async function onAsk(e: FormEvent) {
    e.preventDefault()
    if (!selected || !question.trim()) return
    setAskingQuestion(true)
    setError(null)
    log('Q&A', `Question: ${question}`)
    try {
      const result = await askRepositoryQuestion(selected.id, question.trim())
      setQaResult(result)
      log('Q&A', `Answer received (${result.sources.length} sources)`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Q&A failed'
      setError(msg)
      log('Q&A', `ERROR: ${msg}`)
    } finally {
      setAskingQuestion(false)
    }
  }

  async function onRunAiTests() {
    if (!selected) return
    setRunningAiTests(true)
    setError(null)
    log('Testing', 'Running AI-generated tests…')
    try {
      const result = await runAiTests(selected.id)
      setAiTests(result)
      const [latest, steps] = await Promise.all([getAnalysis(selected.id), getPipeline(selected.id)])
      setAnalysis(latest)
      setPipeline(steps)
      log('Testing', `AI tests ${result.status}: ${result.message}`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'AI tests failed'
      setError(msg)
      log('Testing', `ERROR: ${msg}`)
    } finally {
      setRunningAiTests(false)
    }
  }

  async function onDeployStaging() {
    if (!selected) return
    setDeploying(true)
    setError(null)
    log('Docker/K8s', 'Starting staging deployment…')
    try {
      await deployStaging(selected.id, deploymentTarget)
      const [steps, fresh, deps, workflow] = await Promise.all([
        getPipeline(selected.id), getProject(selected.id), listDeployments(selected.id), getReleaseWorkflow(selected.id),
      ])
      setPipeline(steps)
      setSelected(fresh)
      setDeployments(deps)
      setReleaseWorkflow(workflow)
      setProjects(prev => prev.map(p => p.id === fresh.id ? fresh : p))
      log('Docker/K8s', 'Staging deployment initiated (running in background)')
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Deploy failed'
      setError(msg)
      log('Docker/K8s', `ERROR: ${msg}`)
      if (selected) getPipeline(selected.id).then(setPipeline).catch(() => undefined)
    } finally {
      setDeploying(false)
    }
  }

  async function onCancelStaging() {
    if (!selected) return
    setCancelingStaging(true)
    setError(null)
    try {
      const result = await cancelStaging(selected.id)
      log('Docker/K8s', result.message)
      const [steps, fresh, workflow] = await Promise.all([getPipeline(selected.id), getProject(selected.id), getReleaseWorkflow(selected.id)])
      setPipeline(steps)
      setSelected(fresh)
      setReleaseWorkflow(workflow)
      setProjects(prev => prev.map(p => p.id === fresh.id ? fresh : p))
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Unable to cancel staging'
      setError(msg)
      log('Docker/K8s', `Cancellation error: ${msg}`)
    } finally {
      setCancelingStaging(false)
    }
  }

  async function onRefreshEval() {
    if (!selected) return
    setLoadingEval(true)
    try {
      const result = await getEvaluation(selected.id)
      setEvaluation(result)
      const latest = await getAnalysis(selected.id)
      setAnalysis(latest)
      log('Intelligence', `Evaluation: ${result.retrieval_accuracy}% accuracy`)
    } catch (err) {
      log('Intelligence', `Evaluation error: ${err instanceof Error ? err.message : err}`)
    } finally {
      setLoadingEval(false)
    }
  }

  async function onRequestReleaseApproval() {
    if (!selected) return
    if (!window.confirm('Request manager review for the verified staging release? This creates a pending approval request; it does not start production deployment.')) return
    setDeployingProd(true)
    setError(null)
    try {
      const result = await deployProduction(selected.id)
      log('Release', result.message || `Approval request ${result.status}`)
      window.alert(result.message || `Approval request ${result.status}`)
      const [fresh, workflow] = await Promise.all([getProject(selected.id), getReleaseWorkflow(selected.id)])
      setSelected(fresh); setReleaseWorkflow(workflow)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Approval request could not be submitted'
      setError(message)
      log('Release', `Approval request error: ${message}`)
    } finally {
      setDeployingProd(false)
    }
  }

  // ── derived ─────────────────────────────────────────────────────────────
  const pipelineMap = Object.fromEntries(pipeline.map(s => [s.name, s]))
  const extra = (analysis?.analysis_result ?? {}) as Record<string, unknown>
  const recommendation = extra.deployment_recommendation as {
    summary?: string; potential_issues?: string[]; recommendation?: string[]
  } | undefined
  const testResult = extra.test_result as Record<string, unknown> | undefined
  const securityResult = extra.security_result as {
    findings?: Array<{ severity: string; file: string; message: string }>; summary?: string
  } | undefined
  const dockerResult = extra.docker_result as { message?: string; dockerfile?: string } | undefined
  const deploymentContract = extra.deployment_contract as {
    status?: string; target?: string; preview_path?: string; entrypoint?: string | null; summary?: string
    checks?: Array<{ name: string; status: string; message: string }>
  } | undefined
  const detectedServices = Array.isArray(analysis?.detected_services)
    ? analysis.detected_services.join(', ')
    : Array.isArray(analysis?.analysis_result?.detected_services)
      ? (analysis.analysis_result.detected_services as string[]).join(', ')
      : 'backend, frontend'
  const latestDeployment = deployments[0]
  const latestDeploymentImage = (latestDeployment?.details as { image?: string } | null)?.image
  const smokeResult = (latestDeployment?.details as { smoke?: Record<string, unknown> } | null)?.smoke
  const remoteApplicationUrl = typeof smokeResult?.url === 'string' ? smokeResult.url : null
  const failureAnalysis = (latestDeployment?.details as { failure_analysis?: Record<string, unknown> } | null)?.failure_analysis
  const stagingStep = pipelineMap['Staging']
  const stagingPhase = stagingStep?.result ?? 'Queued for staging deployment'
  const stagingIsActive = selected?.status === 'deploying' || selected?.status === 'staging_cancel_requested' || stagingStep?.status === 'running'

  // ── sidebar ─────────────────────────────────────────────────────────────
  const NAV: { id: Page; label: string; icon: string }[] = [
    { id: 'overview', label: 'Overview', icon: '⬡' },
    { id: 'release', label: 'Release Workflow', icon: '⇢' },
    { id: 'monitoring', label: 'Monitoring', icon: '◉' },
    { id: 'repository', label: 'Repository', icon: '⎇' },
    { id: 'assistant', label: 'AI Assistant', icon: '◈' },
    { id: 'intelligence', label: 'Intelligence', icon: '◉' },
    { id: 'testing', label: 'Testing', icon: '◻' },
    { id: 'docker', label: 'Docker', icon: '▣' },
    { id: 'kubernetes', label: 'Kubernetes', icon: '⬡' },
    { id: 'logs', label: 'Logs', icon: '≡' },
    { id: 'settings', label: 'Settings', icon: '⌗' },
  ]

  return (
    <div className="flex h-screen overflow-hidden bg-zinc-950 text-zinc-200 font-sans">
      {/* ── Sidebar ── */}
      <aside className="flex w-52 shrink-0 flex-col border-r border-zinc-800 bg-zinc-900">
        {/* Brand */}
        <div className="border-b border-zinc-800 px-4 py-4">
          <div className="text-xs tracking-widest text-zinc-500 uppercase">AI DevOps</div>
          <div className="mt-0.5 text-lg font-bold tracking-tight text-white">CodeDeck</div>
        </div>

        {/* Repo selector */}
        <div className="border-b border-zinc-800 px-3 py-3">
          <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-1">Repository</div>
          {selected ? (
            <div className="text-xs text-zinc-300 truncate" title={selected.repository_url}>
              {selected.repository_name}
            </div>
          ) : (
            <div className="text-xs text-zinc-600">None loaded</div>
          )}
          <div className={`mt-1 text-[10px] font-mono ${
            selected?.status === 'ready' || selected?.status === 'staging_healthy'
              ? 'text-emerald-500' : selected?.status === 'failed' ? 'text-red-500'
              : selected?.status === 'analyzing' || selected?.status === 'deploying' ? 'text-yellow-500'
              : 'text-zinc-600'
          }`}>
            {selected?.status ?? 'idle'}
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto py-2">
          {NAV.map(({ id, label, icon }) => (
            <button
              key={id}
              onClick={() => setPage(id)}
              className={`flex w-full items-center gap-2.5 px-4 py-2 text-xs transition-colors ${
                page === id
                  ? 'bg-zinc-800 text-white'
                  : 'text-zinc-500 hover:bg-zinc-800/50 hover:text-zinc-300'
              }`}
            >
              <span className="text-base">{icon}</span>
              {label}
            </button>
          ))}
        </nav>

        {/* API status */}
        <div className="border-t border-zinc-800 px-4 py-3">
          <div className={`flex items-center gap-2 text-[10px] ${apiOk ? 'text-emerald-500' : apiOk === false ? 'text-red-500' : 'text-zinc-600'}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${apiOk ? 'bg-emerald-500' : apiOk === false ? 'bg-red-500' : 'bg-zinc-600'}`} />
            API {apiOk === null ? '…' : apiOk ? 'online' : 'offline'}
          </div>
        </div>
      </aside>

      {/* ── Main content ── */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top bar */}
        <header className="flex shrink-0 items-center justify-between border-b border-zinc-800 bg-zinc-900 px-6 py-3">
          <div className="flex items-center gap-3">
            <span className="text-xs uppercase tracking-widest text-zinc-500">{NAV.find(n => n.id === page)?.label}</span>
          </div>
          <div className="flex items-center gap-3">
            {selected && (
              <span className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-300">
                {selected.repository_name}
              </span>
            )}
            {selected && (
              <div className="flex gap-2">
                <button className="rounded border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:bg-zinc-800" onClick={async () => {
                  if (!window.confirm('Clear the cloned repository and temporary deployment copies? Analysis results, generated files, deployment history, and Kubernetes resources will remain.')) return
                  setError(null)
                  try { const result = await clearProjectWorkspace(selected.id); window.alert(result.message); }
                  catch (err) { setError(err instanceof Error ? err.message : 'Workspace cleanup failed') }
                }}>Clear Repository Files</button>
                <button className="rounded border border-amber-800 px-2 py-1 text-[11px] text-amber-300 hover:bg-amber-950/30" onClick={async () => {
                  const owned = deployments.filter(d => ['remote_staging', 'staging'].includes(d.deployment_type) && ['SUCCEEDED', 'healthy', 'staging_healthy', 'FAILED', 'failed'].includes(d.status))
                  if (!owned.length) { setError('No safely identifiable completed staging deployment is available to undeploy.'); return }
                  const inventory = owned.map(d => {
                    const details = (d.details ?? {}) as { namespace?: string }
                    return `#${d.id} · namespace ${details.namespace ?? 'unknown'}`
                  }).join('\n')
                  if (!window.confirm(`Remove these application resources?\n${inventory}\n\nDeployments, services, ingress, configuration, and access bindings will be removed. Persistent volume claims and their data will be preserved. Remote cleanup may remain queued until the worker reports completion.`)) return
                  setError(null)
                  try {
                    const results = []
                    for (const deployment of owned) results.push(await cleanupDeployment(selected.id, deployment.id))
                    window.alert(results.map(r => r.message).join('\n'))
                    await loadProject(selected)
                  } catch (err) { setError(err instanceof Error ? err.message : 'Undeploy did not complete') }
                }}>Undeploy Application</button>
                <button className="rounded border border-red-900 px-2 py-1 text-[11px] text-red-300 hover:bg-red-950/30" onClick={async () => {
                  const name = window.prompt(`Permanently delete ${selected.repository_name}? This removes all associated analysis, generated files, pipeline steps, and deployment history. Type the project name to continue.`)
                  if (name === null) return
                  setError(null)
                  try {
                    const result = await deleteProject(selected.id, name)
                    window.alert(result.message)
                    setSelected(null)
                    setAnalysis(null); setFiles([]); setDeployments([])
                    setReleaseWorkflow(null)
                    setProjects(await listProjects())
                  } catch (err) { setError(err instanceof Error ? err.message : 'Project deletion failed') }
                }}>Delete Project</button>
              </div>
            )}
            {selected && files.length > 0 && (
              <>
                <button
                  onClick={() => { setPage('kubernetes'); onDeployStaging() }}
                  disabled={deploying || busy}
                  className="rounded bg-sky-700 px-3 py-1 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50"
                >
                  {deploying ? 'Deploying…' : 'Deploy Staging'}
                </button>
              </>
            )}
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-6">
          {error && (
            <div className="mb-4 rounded border border-red-700/50 bg-red-950/40 px-4 py-3 text-sm text-red-300">
              <span className="font-semibold">Action needed: </span>{friendlyError(error)}
              <button onClick={() => setError(null)} className="ml-3 text-red-500 hover:text-red-300">✕</button>
            </div>
          )}

          {/* ── OVERVIEW ── */}
          {page === 'overview' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>GitHub Repository</SectionTitle>
              <form onSubmit={onAnalyze} className="flex gap-2">
                <input
                  type="url"
                  required
                  value={url}
                  onChange={e => setUrl(e.target.value)}
                  placeholder="https://github.com/owner/repo"
                  className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-4 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-sky-500"
                />
                <button
                  type="submit"
                  disabled={busy}
                  className="rounded bg-sky-700 px-5 py-2.5 text-sm font-medium text-white hover:bg-sky-600 disabled:opacity-50"
                >
                  {busy ? 'Analyzing…' : 'Analyze'}
                </button>
              </form>

              {projects.length > 0 && (
                <>
                  <SectionTitle>Recent Projects</SectionTitle>
                  <div className="space-y-1.5">
                    {projects.slice(0, 8).map(p => (
                      <button
                        key={p.id}
                        onClick={() => loadProject(p, true).catch(err => setError(String(err.message || err)))}
                        className={`flex w-full items-center justify-between rounded border px-3 py-2 text-left text-xs transition ${
                          selected?.id === p.id ? 'border-sky-700 bg-sky-950/40 text-sky-300' : 'border-zinc-800 hover:border-zinc-700 text-zinc-400 hover:text-zinc-200'
                        }`}
                      >
                        <span className="font-medium">{p.repository_name}</span>
                        <span className={`ml-2 rounded px-1.5 py-0.5 text-[10px] ${
                          p.status === 'ready' || p.status === 'staging_healthy' ? 'bg-emerald-900 text-emerald-400'
                          : p.status === 'failed' ? 'bg-red-900 text-red-400'
                          : 'bg-zinc-800 text-zinc-500'
                        }`}>{p.status}</span>
                      </button>
                    ))}
                  </div>
                </>
              )}

              {selected && (
                <>
                  <SectionTitle>Pipeline Status</SectionTitle>
                  <div className="space-y-1">
                    {PIPELINE_STEPS.map(name => {
                      const step = pipelineMap[name]
                      const s = step?.status ?? 'pending'
                      return (
                        <div key={name} className="flex items-start gap-3 rounded border border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs">
                          <span className={`shrink-0 font-bold ${statusColor(s)}`}>{stepIcon(s)}</span>
                          <div className="flex-1 min-w-0">
                            <div className="flex justify-between">
                              <span className="text-zinc-300">{name}</span>
                              <span className={`ml-2 shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${statusBg(s)}`}>{s}</span>
                            </div>
                            {step?.result && <div className="mt-0.5 truncate text-zinc-500">{step.result}</div>}
                            {step?.error && <div className="mt-0.5 truncate text-red-400">{friendlyError(step.error)}</div>}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </>
              )}
            </div>
          )}

          {page === 'monitoring' && (
            <div className="space-y-6 max-w-[90rem]">
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div>
                  <SectionTitle>CodeDeck Monitoring</SectionTitle>
                  <p className="text-[11px] text-zinc-500">Repository activity uses Watchdog. Kubernetes health and metrics use Kubernetes APIs and Metrics Server when available.</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <label className="text-[10px] text-zinc-500">Project
                    <select value={monitoringProjectFilter} onChange={e => { setMonitoringProjectFilter(e.target.value); setMonitoringDeploymentFilter('all') }} className="ml-2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200">
                      <option value="all">All projects</option>
                      {projects.map(project => <option key={project.id} value={project.id}>{project.repository_name}</option>)}
                    </select>
                  </label>
                  <label className="text-[10px] text-zinc-500">Deployment
                    <select value={monitoringDeploymentFilter} onChange={e => setMonitoringDeploymentFilter(e.target.value)} className="ml-2 max-w-48 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200">
                      <option value="all">All deployments</option>
                      {(monitoringData?.deployment_events ?? []).map(deployment => <option key={deployment.id} value={deployment.id}>#{deployment.id} · {deployment.environment} · {deployment.status}</option>)}
                    </select>
                  </label>
                  <label className="text-[10px] text-zinc-500">Time
                    <select value={monitoringTimeFilter} onChange={e => setMonitoringTimeFilter(e.target.value)} className="ml-2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200">
                      <option value="1h">Last hour</option><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="all">All time</option>
                    </select>
                  </label>
                  <button onClick={() => void loadMonitoring()} className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800">{monitoringLoading ? 'Refreshing…' : 'Refresh'}</button>
                </div>
              </div>

              {monitoringError && <div className="rounded border border-red-800/60 bg-red-950/30 px-4 py-3 text-xs text-red-300">{monitoringError}</div>}
              {monitoringData && (
                <>
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                    {Object.entries(monitoringData.system_health).map(([name, health]) => (
                      <div key={name} className="rounded border border-zinc-800 bg-zinc-900/60 p-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[10px] uppercase tracking-widest text-zinc-500">{name.replace('_', ' ')}</span>
                          <span className={`rounded border px-1.5 py-0.5 text-[9px] uppercase ${health.status === 'available' ? 'border-emerald-800 text-emerald-300' : health.status === 'not_required' ? 'border-zinc-700 text-zinc-400' : 'border-amber-800 text-amber-300'}`}>{health.status}</span>
                        </div>
                        <div className="mt-2 text-[10px] text-zinc-400">{health.message}</div>
                        {health.sampled_at && <div className="mt-1 text-[9px] text-zinc-600">Sample: {formatWorkflowTime(health.sampled_at)}</div>}
                      </div>
                    ))}
                    <div className="rounded border border-zinc-800 bg-zinc-900/60 p-3">
                      <div className="flex items-center justify-between gap-2"><span className="text-[10px] uppercase tracking-widest text-zinc-500">Monitoring service</span><span className={`rounded border px-1.5 py-0.5 text-[9px] uppercase ${monitoringData.monitoring_service.status === 'available' ? 'border-emerald-800 text-emerald-300' : 'border-amber-800 text-amber-300'}`}>{monitoringData.monitoring_service.status}</span></div>
                      <div className="mt-2 text-[10px] text-zinc-400">Watchdog {monitoringData.monitoring_service.observer_running ? 'running' : 'stopped'} · {monitoringData.monitoring_service.monitored_project_count} registered project(s)</div>
                      <div className="mt-1 text-[9px] text-zinc-600">Kubernetes mode: {monitoringData.monitoring_service.kubernetes_mode} · Last file event: {formatWorkflowTime(monitoringData.monitoring_service.last_event_at)}</div>
                      {monitoringData.monitoring_service.last_error && <div className="mt-2 text-[10px] text-amber-300">{monitoringData.monitoring_service.last_error}</div>}
                    </div>
                  </div>

                  <div className="grid gap-5 xl:grid-cols-2">
                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <div className="mb-3 flex flex-wrap items-center justify-between gap-3"><SectionTitle>Repository File Activity</SectionTitle>
                        {(monitoringProjectFilter !== 'all' || selected) && <button disabled={busy} onClick={() => void onManualReanalysis()} className="rounded border border-sky-800 px-3 py-1.5 text-[10px] text-sky-300 hover:bg-sky-950/40 disabled:opacity-50">{busy ? 'Analysis running…' : 'Run fresh analysis'}</button>}
                      </div>
                      <p className="mb-3 text-[10px] text-zinc-600">File contents are never stored. Changes are recorded with their CodeDeck workflow context; they do not trigger a deployment automatically.</p>
                      {monitoringData.file_activity.length ? <div className="max-h-[28rem] overflow-auto rounded border border-zinc-800">
                        {monitoringData.file_activity.map(event => <div key={event.id} className="grid gap-1 border-b border-zinc-900 px-3 py-2 sm:grid-cols-[7rem_1fr_auto] sm:items-center">
                          <span className="text-[9px] text-zinc-600">{formatWorkflowTime(event.occurred_at)}</span>
                          <div className="min-w-0"><div className="break-all font-mono text-[10px] text-zinc-300">{event.path}</div><div className="mt-0.5 text-[9px] text-zinc-600">{event.summary ?? event.event_type} · {event.workflow_context} · {event.processing_status}</div></div>
                          <span className={`w-fit rounded border px-1.5 py-0.5 text-[9px] uppercase ${event.event_type === 'deleted' ? 'border-red-900 text-red-300' : 'border-zinc-700 text-zinc-400'}`}>{event.event_type}</span>
                        </div>)}
                      </div> : <p className="py-6 text-center text-xs text-zinc-600">No repository events in this time range.</p>}
                    </section>

                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <SectionTitle>Deployment Rollout Health</SectionTitle>
                      {monitoringData.kubernetes.status !== 'available' && <p className="mb-3 rounded border border-amber-900/50 bg-amber-950/20 p-3 text-xs text-amber-300">{monitoringData.kubernetes.message ?? 'Kubernetes monitoring is unavailable.'}</p>}
                      {!!monitoringData.kubernetes.nodes?.length && <div className="mb-3 flex flex-wrap gap-2">{monitoringData.kubernetes.nodes.map(node => <span key={node.name} className={`rounded border px-2 py-1 text-[9px] ${node.ready ? 'border-emerald-900 text-emerald-300' : 'border-red-900 text-red-300'}`}>Node {node.name}: {node.ready ? 'Ready' : node.reason ?? 'NotReady'}</span>)}</div>}
                      <div className="space-y-2">
                        {monitoringData.kubernetes.deployments.map(deployment => <div key={`${deployment.namespace}/${deployment.name}`} className="rounded border border-zinc-800 p-3">
                          <div className="flex flex-wrap items-center justify-between gap-2 text-xs"><span className="font-mono text-zinc-200">{deployment.namespace}/{deployment.name}</span><span className={`rounded border px-1.5 py-0.5 text-[9px] uppercase ${deployment.status === 'healthy' ? 'border-emerald-800 text-emerald-300' : deployment.status === 'unhealthy' ? 'border-red-800 text-red-300' : 'border-amber-800 text-amber-300'}`}>{deployment.status}</span></div>
                          <div className="mt-2 text-[10px] text-zinc-500">Ready {deployment.ready_replicas}/{deployment.desired_replicas} · Available {deployment.available_replicas} · Updated {deployment.updated_replicas}{deployment.reason ? ` · ${deployment.reason}` : ''}</div>
                        </div>)}
                        {monitoringData.kubernetes.deployments.length === 0 && monitoringData.kubernetes.status === 'available' && <p className="py-3 text-xs text-zinc-600">No monitored Kubernetes deployments in the selected scope.</p>}
                      </div>
                      <SectionTitle>Pods and Autoscaling</SectionTitle>
                      <div className="max-h-72 space-y-2 overflow-auto">
                        {monitoringData.kubernetes.pods.map(pod => <div key={`${pod.namespace}/${pod.name}`} className="flex flex-wrap items-center justify-between gap-2 rounded border border-zinc-800 px-3 py-2 text-[10px]">
                          <span className="min-w-0 break-all font-mono text-zinc-300">{pod.namespace}/{pod.name}</span><span className={pod.ready && !pod.reason ? 'text-emerald-300' : 'text-amber-300'}>{pod.ready ? 'Ready' : pod.reason ?? pod.phase}</span><span className={pod.restart_count >= 3 ? 'text-red-300' : 'text-zinc-500'}>Restarts {pod.restart_count}</span>
                        </div>)}
                        {monitoringData.kubernetes.pods.length === 0 && monitoringData.kubernetes.status === 'available' && <p className="text-xs text-zinc-600">No pods in the selected scope.</p>}
                        {monitoringData.kubernetes.hpas.map(hpa => <div key={`${hpa.namespace}/${hpa.name}`} className="rounded border border-zinc-800 px-3 py-2 text-[10px] text-zinc-400">HPA {hpa.namespace}/{hpa.name} · replicas {hpa.current_replicas}/{hpa.desired_replicas} · range {hpa.min_replicas}–{hpa.max_replicas ?? '—'}</div>)}
                      </div>
                    </section>
                  </div>

                  <div className="grid gap-5 xl:grid-cols-2">
                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <SectionTitle>Resource Usage</SectionTitle>
                      {monitoringData.kubernetes.metrics.status === 'available' ? <>
                        <div className="mb-3 text-[10px] text-zinc-500">Metrics Server sample: {formatWorkflowTime(monitoringData.kubernetes.metrics.sampled_at)}</div>
                        {monitoringData.kubernetes.metrics.pods.map(metric => <div key={`${metric.namespace}/${metric.name}`} className="flex flex-wrap justify-between gap-2 border-b border-zinc-900 py-2 text-[10px]"><span className="font-mono text-zinc-300">{metric.namespace}/{metric.name}</span><span className="text-zinc-400">CPU {metric.cpu_millicores}m · Memory {formatBytes(metric.memory_bytes)}</span></div>)}
                        {monitoringData.kubernetes.metrics.nodes.map(metric => <div key={metric.name} className="flex flex-wrap justify-between gap-2 border-b border-zinc-900 py-2 text-[10px]"><span className="font-mono text-zinc-300">Node {metric.name}</span><span className="text-zinc-400">CPU {metric.cpu_millicores == null ? 'Not available' : `${metric.cpu_millicores}m`} · Memory {formatBytes(metric.memory_bytes)}</span></div>)}
                        {!monitoringData.kubernetes.metrics.pods.length && !monitoringData.kubernetes.metrics.nodes.length && <p className="text-xs text-zinc-600">Metrics Server returned no usage records for this scope.</p>}
                      </> : <p className="rounded border border-amber-900/50 bg-amber-950/20 p-3 text-xs text-amber-300">{monitoringData.kubernetes.metrics.message || 'Metrics Server is unavailable; no CPU or memory values are reported.'}</p>}
                    </section>

                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <SectionTitle>Recent Deployment Events</SectionTitle>
                      {monitoringData.deployment_events.length ? <div className="space-y-2">
                        {monitoringData.deployment_events.map(event => <details key={event.id} className="rounded border border-zinc-800 p-3">
                          <summary className="flex cursor-pointer flex-wrap items-center justify-between gap-2 text-[10px]"><span className="text-zinc-300">#{event.id} · {event.environment} · {event.status}</span><span className="text-zinc-600">{formatWorkflowTime(event.created_at)}</span></summary>
                          {event.logs.length > 0 && <TerminalBlock text={event.logs.map(line => `${line.time ?? ''} ${line.message}`.trim()).join('\n')} maxH="8rem" />}
                          {!event.logs.length && <div className="mt-2 text-[10px] text-zinc-600">No persisted log lines for this deployment record.</div>}
                        </details>)}
                      </div> : <p className="text-xs text-zinc-600">No deployment events in the selected time range.</p>}
                    </section>
                  </div>

                  <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                    <SectionTitle>Alert History</SectionTitle>
                    {monitoringData.alerts.length ? <div className="space-y-2">
                      {monitoringData.alerts.map(alert => <div key={alert.id} className="grid gap-2 rounded border border-zinc-800 p-3 sm:grid-cols-[8rem_1fr_auto] sm:items-start">
                        <div className="text-[9px] text-zinc-600">{formatWorkflowTime(alert.last_seen)}</div>
                        <div><div className="text-xs font-semibold text-zinc-200">{alert.title}</div><div className="mt-1 text-[10px] text-zinc-400">{alert.message}</div><div className="mt-1 text-[9px] text-zinc-600">{alert.source} · {alert.occurrence_count} occurrence(s)</div></div>
                        <span className={`w-fit rounded border px-1.5 py-0.5 text-[9px] uppercase ${alert.is_resolved ? 'border-zinc-700 text-zinc-500' : alert.severity === 'critical' ? 'border-red-800 text-red-300' : 'border-amber-800 text-amber-300'}`}>{alert.is_resolved ? 'Resolved' : alert.severity}</span>
                      </div>)}
                    </div> : <p className="text-xs text-zinc-600">No alerts in the selected time range.</p>}
                  </section>
                </>
              )}
            </div>
          )}

          {page === 'release' && (
            <div className="space-y-6 max-w-[90rem]">
              <SectionTitle>Release Overview</SectionTitle>
              {!selected ? (
                <p className="text-sm text-zinc-500">Choose a project to view its release workflow.</p>
              ) : !releaseWorkflow ? (
                <p className="text-sm text-zinc-500">Workflow history is unavailable. Refresh the project to load persisted pipeline and deployment records.</p>
              ) : (
                <>
                  {releaseWorkflow.release.status === 'staging_healthy' && deploymentTarget === 'local' && systemStatus?.deployment_target === 'local' && (
                    <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-sky-800/40 bg-sky-950/20 p-4">
                      <div><div className="text-sm font-medium text-sky-200">Staging verified</div><p className="mt-1 text-xs text-zinc-400">Request manager review for this release. Production will not start until a manager decision is recorded.</p></div>
                      <button onClick={onRequestReleaseApproval} disabled={deployingProd} className="rounded bg-sky-700 px-4 py-2 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50">{deployingProd ? 'Requesting…' : 'Request production approval'}</button>
                    </div>
                  )}
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                    {[
                      ['Repository', releaseWorkflow.release.repository_name],
                      ['Branch', releaseWorkflow.release.branch ?? 'Not recorded'],
                      ['Commit', releaseWorkflow.release.commit_id ? releaseWorkflow.release.commit_id.slice(0, 12) : 'Not recorded'],
                      ['Commit message', releaseWorkflow.release.commit_message ?? 'Not recorded'],
                      ['Environment', releaseWorkflow.release.environment ?? 'Not deployed'],
                      ['Version', releaseWorkflow.release.version ?? 'Not assigned'],
                      ['Release status', releaseWorkflow.release.status],
                      ['Approval', releaseWorkflow.release.approval_status],
                      ['Workflow elapsed', formatDuration(releaseWorkflow.analytics.total_duration_seconds)],
                    ].map(([label, value]) => (
                      <div key={label} className="min-w-0 rounded border border-zinc-800 bg-zinc-900/60 p-3">
                        <div className="text-[10px] uppercase tracking-widest text-zinc-600">{label}</div>
                        <div className="mt-1 truncate text-xs text-zinc-200" title={value}>{value}</div>
                      </div>
                    ))}
                  </div>

                  <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                    <div className="mb-1 text-[10px] uppercase tracking-widest text-zinc-500">Delivery workflow</div>
                    <p className="mb-4 text-[11px] text-zinc-500">Development through production promotion. Pull request and external CI integrations are not connected; staging and production actions are manually triggered.</p>
                    <div className="flex flex-col gap-3 overflow-x-auto pb-2 md:flex-row md:items-stretch">
                      {releaseWorkflow.stages.map((stage, index) => (
                        <div key={stage.id} className="flex min-w-0 items-stretch gap-3 md:w-auto md:flex-none">
                          <article className={`w-full rounded border p-3 md:w-64 ${workflowStatusClass(stage.status)}`}>
                            <div className="flex items-start justify-between gap-2">
                              <h3 className="text-xs font-semibold leading-5">{index + 1}. {stage.name}</h3>
                              <span className="shrink-0 rounded border border-current/30 px-1.5 py-0.5 text-[9px] uppercase">{stage.status}</span>
                            </div>
                            <div className="mt-2 text-[10px] text-zinc-400">Duration: {formatDuration(stage.duration_seconds)}</div>
                            {stage.waiting_seconds != null && <div className="mt-1 text-[10px] text-zinc-400">Waiting: {formatDuration(stage.waiting_seconds)}</div>}
                            {stage.execution_seconds != null && <div className="mt-1 text-[10px] text-zinc-400">Execution: {formatDuration(stage.execution_seconds)}</div>}
                            <details className="mt-3 border-t border-current/20 pt-2">
                              <summary className="cursor-pointer text-[10px] font-medium">Stage details</summary>
                              <div className="mt-2 space-y-2 text-[10px] text-zinc-300">
                                <div><span className="text-zinc-500">Responsible:</span> {stage.actor}</div>
                                <div><span className="text-zinc-500">Depends on:</span> {stage.dependencies.length ? stage.dependencies.map(id => releaseWorkflow.stages.find(item => item.id === id)?.name ?? id).join(', ') : 'None'}</div>
                                <div><span className="text-zinc-500">Started:</span> {formatWorkflowTime(stage.started_at)}</div>
                                <div><span className="text-zinc-500">Completed:</span> {formatWorkflowTime(stage.completed_at)}</div>
                                {stage.output && <p className="whitespace-pre-wrap break-words text-zinc-400">{stage.output}</p>}
                                {stage.logs.length > 0 && <div className="max-h-36 space-y-1 overflow-auto rounded bg-zinc-950/70 p-2 font-mono text-[9px]">
                                  {stage.logs.map((entry, logIndex) => <div key={logIndex} className="whitespace-pre-wrap break-words">{typeof entry === 'string' ? entry : `${entry.time ?? ''} ${entry.message ?? ''}`.trim()}</div>)}
                                </div>}
                              </div>
                            </details>
                          </article>
                          {index < releaseWorkflow.stages.length - 1 && (
                            <div className="flex w-full items-center justify-center text-zinc-600 md:w-7">
                              <span className="md:hidden">↓</span><span className="hidden md:inline">→</span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                    <p className="mt-2 text-[10px] text-zinc-600">{releaseWorkflow.analytics.duration_basis} Missing timestamps are shown as not recorded.</p>
                  </div>

                  <div className="grid gap-5 xl:grid-cols-2">
                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <SectionTitle>Repository Snapshot and File Changes</SectionTitle>
                      <p className="mb-3 text-xs text-zinc-400">{releaseWorkflow.repository_snapshot.change_summary}</p>
                      {releaseWorkflow.repository_snapshot.snapshot_available ? (
                        <>
                          <div className="mb-3 flex flex-wrap gap-2 text-[10px] text-zinc-500">
                            <span>{releaseWorkflow.repository_snapshot.file_count ?? releaseWorkflow.repository_snapshot.files.length} tracked files discovered</span>
                            <span>·</span><span>Added: {releaseWorkflow.repository_snapshot.added.length}</span>
                            <span>·</span><span>Modified: {releaseWorkflow.repository_snapshot.modified.length}</span>
                            <span>·</span><span>Deleted: {releaseWorkflow.repository_snapshot.deleted.length}</span>
                          </div>
                          <div className="max-h-72 overflow-auto rounded border border-zinc-800 bg-zinc-950/70">
                            {releaseWorkflow.repository_snapshot.files.map(path => <div key={path} className="border-b border-zinc-900 px-3 py-1.5 font-mono text-[10px] text-zinc-300">{path}</div>)}
                            {releaseWorkflow.repository_snapshot.files.length === 0 && <div className="p-3 text-xs text-zinc-600">No tracked file inventory was captured.</div>}
                          </div>
                          <p className="mt-3 text-[10px] text-zinc-600">No stage-to-file impact mapping is persisted. Change comparisons are not inferred from file paths.</p>
                        </>
                      ) : <p className="text-xs text-zinc-600">No Git snapshot metadata is stored for this analysis.</p>}
                    </section>

                    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
                      <SectionTitle>Deployment History</SectionTitle>
                      {releaseWorkflow.deployment_history.length ? <div className="space-y-3">
                        {releaseWorkflow.deployment_history.map(deployment => {
                          const info = (deployment.details ?? {}) as {
                            target?: string; namespace?: string; image?: string; app_name?: string; application_port?: number;
                            ingress_hostname?: string; failure_reason?: string; started_at?: string; completed_at?: string;
                            logs?: Array<{ time?: string; message?: string }>; smoke?: { status?: string; url?: string | null; message?: string };
                            result?: { deployment_status?: { ready_replicas?: number; replicas?: number } };
                          }
                          return <article key={deployment.id} className="rounded border border-zinc-800 p-3">
                            <div className="flex flex-wrap justify-between gap-2 text-xs">
                              <span className="font-semibold text-zinc-200">#{deployment.id} · {deployment.environment} · {deployment.status}</span>
                              <span className="text-zinc-500">{formatWorkflowTime(deployment.created_at)}</span>
                            </div>
                            <div className="mt-2 grid gap-1 text-[10px] text-zinc-400 sm:grid-cols-2">
                              <span>Target: {info.target ?? deployment.deployment_type}</span><span>Namespace: {info.namespace ?? 'Not recorded'}</span>
                              <span>Image: {info.image ?? 'Not recorded'}</span><span>Application: {info.app_name ?? 'Not recorded'}</span>
                              <span>Port: {info.application_port ?? 'Not recorded'}</span>
                              <span>Ready replicas: {info.result?.deployment_status?.ready_replicas ?? 'Not recorded'}/{info.result?.deployment_status?.replicas ?? 'Not recorded'}</span>
                              <span>Smoke test: {info.smoke?.status ?? 'Not recorded'}</span><span>URL: {info.smoke?.url ?? 'Not recorded'}</span>
                              <span>Started: {formatWorkflowTime(info.started_at)}</span><span>Completed: {formatWorkflowTime(info.completed_at)}</span>
                            </div>
                            {info.failure_reason && <p className="mt-2 text-[10px] text-red-400">{info.failure_reason}</p>}
                            {info.logs?.length ? <details className="mt-2"><summary className="cursor-pointer text-[10px] text-zinc-500">Deployment logs ({info.logs.length})</summary><TerminalBlock text={info.logs.map(line => `${line.time ?? ''} ${line.message ?? ''}`.trim()).join('\n')} maxH="10rem" /></details> : null}
                          </article>
                        })}
                      </div> : <p className="text-xs text-zinc-600">No deployment records are available.</p>}
                    </section>
                  </div>
                </>
              )}
            </div>
          )}

          {/* ── REPOSITORY ── */}
          {page === 'repository' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>Repository Details</SectionTitle>
              {!selected ? (
                <p className="text-sm text-zinc-500">No repository loaded. Use Overview to analyze one.</p>
              ) : (
                <>
                  <div className="rounded border border-zinc-800 bg-zinc-900/60 px-4 py-3">
                    <div className="text-[10px] uppercase tracking-widest text-zinc-600">URL</div>
                    <div className="mt-1 break-all text-xs text-sky-400">{selected.repository_url}</div>
                  </div>
                  {analysis ? (
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                      <Pill label="Language" value={analysis.language} />
                      <Pill label="Framework" value={analysis.framework} />
                      <Pill label="Package Manager" value={analysis.package_manager} />
                      <Pill label="Entry Point" value={analysis.entrypoint} />
                      <Pill label="Port" value={String(extra.port ?? '—')} />
                      <Pill label="Test Command" value={analysis.test_command} />
                      <Pill label="Dockerfile" value={analysis.has_dockerfile ? 'Present' : 'Not found'} />
                      <Pill label="Security Score" value={analysis.security_score != null ? `${analysis.security_score}/100` : null} />
                    </div>
                  ) : (
                    <p className="text-sm text-zinc-500">Run analysis to see repository details.</p>
                  )}

                  {(extra.multiservice_notice as string | undefined) && (
                    <div className="rounded border border-sky-700/30 bg-sky-950/30 p-3 text-xs text-sky-300">
                      {String(extra.multiservice_notice)}
                    </div>
                  )}

                  {files.length > 0 && (
                    <>
                      <SectionTitle>Generated Files</SectionTitle>
                      <div className="flex gap-3">
                        <ul className="w-36 shrink-0 space-y-1">
                          {files.map(f => (
                            <li key={f.filename}>
                              <button
                                onClick={() => setSelectedFile(f.filename)}
                                className={`w-full rounded px-2 py-1 text-left text-xs transition ${
                                  selectedFile === f.filename ? 'bg-sky-900/40 text-sky-300' : 'text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300'
                                }`}
                              >
                                {f.filename}
                              </button>
                            </li>
                          ))}
                        </ul>
                        <TerminalBlock text={files.find(f => f.filename === selectedFile)?.content ?? ''} maxH="24rem" />
                      </div>
                    </>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── AI ASSISTANT ── */}
          {page === 'assistant' && (
            <div className="flex h-full max-h-[calc(100vh-8rem)] flex-col gap-4 max-w-3xl">
              <SectionTitle>CodeDeck AI Assistant</SectionTitle>
              {!selected ? (
                <p className="text-sm text-zinc-500">Load a repository first.</p>
              ) : (
                <>
                  <div className="text-xs text-zinc-500">
                    Ask questions grounded in the indexed repository. Answers cite retrieved source files.
                  </div>

                  <form onSubmit={onAsk} className="flex gap-2">
                    <input
                      type="text"
                      value={question}
                      onChange={e => setQuestion(e.target.value)}
                      placeholder="Where is authentication implemented?"
                      className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-4 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-sky-500"
                    />
                    <button
                      type="submit"
                      disabled={askingQuestion}
                      className="rounded bg-sky-700 px-5 py-2.5 text-sm font-medium text-white hover:bg-sky-600 disabled:opacity-50"
                    >
                      {askingQuestion ? 'Searching…' : 'Ask'}
                    </button>
                  </form>

                  {/* Quick questions */}
                  <div className="flex flex-wrap gap-2">
                    {[
                      'What framework does this project use?',
                      'What is the application entry point?',
                      'What database is being used?',
                      'What port does the application use?',
                      'How is the project structured?',
                    ].map(q => (
                      <button
                        key={q}
                        onClick={() => setQuestion(q)}
                        className="rounded border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-400 hover:border-sky-700 hover:text-sky-300"
                      >
                        {q}
                      </button>
                    ))}
                  </div>

                  {qaResult && (
                    <div className="flex flex-1 gap-4 overflow-hidden">
                      <div className="flex-1 overflow-y-auto space-y-3">
                        <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4">
                          <div className="flex items-center justify-between mb-2">
                            <div className="text-[10px] uppercase tracking-widest text-zinc-500 font-semibold">Answer</div>
                            {qaResult.provider && (
                              <span className="rounded bg-sky-950/80 border border-sky-800/60 px-2 py-0.5 text-[10px] text-sky-300 font-mono">
                                Engine: {qaResult.provider}
                              </span>
                            )}
                          </div>
                          <div className="mb-3 text-[10px] text-zinc-500">
                            Retrieval: {qaResult.retrieval_method ?? 'unknown'} · {qaResult.retrieval_count ?? qaResult.sources.length} sources · {qaResult.retrieval_latency_ms == null ? 'latency unavailable' : `${qaResult.retrieval_latency_ms.toFixed(1)} ms`}
                          </div>
                          <FormattedText text={qaResult.answer} />
                        </div>
                        {qaResult.sources.length > 0 && (
                          <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4">
                            <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-3">Retrieved Evidence</div>
                            <div className="space-y-3">
                              {qaResult.sources.map(src => (
                                <div key={`${src.path}-${src.line_start}`} className="text-xs">
                                  <div className="font-mono text-sky-400">
                                    {src.path}:{src.line_start}–{src.line_end}
                                    <span className="ml-2 text-zinc-600">score {src.score.toFixed(2)}</span>
                                  </div>
                                  <p className="mt-1 text-zinc-400 leading-relaxed">{src.snippet}</p>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── REPOSITORY INTELLIGENCE ── */}
          {page === 'intelligence' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>Repository Intelligence</SectionTitle>
              {!selected || !analysis ? (
                <p className="text-sm text-zinc-500">Analyze a repository to see intelligence data.</p>
              ) : (
                <>
                  {/* Recommendation */}
                  {recommendation && (
                    <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4 space-y-3">
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600">Deployment Recommendation</div>
                      <FormattedText text={recommendation.summary} />
                      {(recommendation.potential_issues ?? []).length > 0 && (
                        <div>
                          <div className="text-[10px] uppercase tracking-widest text-orange-500 mb-1">Potential Issues</div>
                          <ul className="space-y-0.5">
                            {recommendation.potential_issues!.map(i => (
                              <li key={i} className="text-xs text-orange-300">⚠ {i}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {(recommendation.recommendation ?? []).length > 0 && (
                        <ul className="space-y-0.5">
                          {recommendation.recommendation!.map(i => (
                            <li key={i} className="text-xs text-zinc-400">→ {i}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {/* RAG index info */}
                  {(extra.repository_index as { file_count?: number; chunk_count?: number } | undefined) && (
                    <div className="grid grid-cols-2 gap-3">
                      <Pill label="Indexed Files" value={(extra.repository_index as { file_count?: number }).file_count} />
                      <Pill label="Index Chunks" value={(extra.repository_index as { chunk_count?: number }).chunk_count} />
                    </div>
                  )}

                  {/* Evaluation */}
                  <div className="flex items-center justify-between">
                    <div className="text-[10px] uppercase tracking-widest text-zinc-600">Retrieval Evaluation</div>
                    <button
                      onClick={onRefreshEval}
                      disabled={loadingEval}
                      className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-400 hover:bg-zinc-800 disabled:opacity-50"
                    >
                      {loadingEval ? 'Refreshing…' : 'Refresh'}
                    </button>
                  </div>
                  {evaluation && (
                    <div className="space-y-2">
                      <div className="flex gap-4 text-xs">
                        <span className="text-zinc-400">{evaluation.correct}/{evaluation.questions} correct</span>
                        <span className="text-emerald-400 font-bold">{evaluation.retrieval_accuracy}% accuracy</span>
                      </div>
                      {evaluation.results.map(item => (
                        <div key={item.question} className="rounded border border-zinc-800 px-3 py-2 text-xs">
                          <div className="flex justify-between">
                            <span className="text-zinc-300">{item.question}</span>
                            <span className={item.status === 'correct' ? 'text-emerald-400' : 'text-orange-400'}>{item.status}</span>
                          </div>
                          <div className="mt-0.5 text-zinc-500 truncate">{item.answer}</div>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Security */}
                  {securityResult && (
                    <>
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mt-2">Security Scan</div>
                      <div className="text-xs text-zinc-400">{securityResult.summary}</div>
                      {(securityResult.findings ?? []).length > 0 && (
                        <div className="space-y-1">
                          {securityResult.findings!.map((f, i) => (
                            <div key={i} className="flex gap-2 rounded border border-zinc-800 px-3 py-1.5 text-xs">
                              <span className={f.severity === 'HIGH' ? 'text-red-400' : 'text-orange-400'}>{f.severity}</span>
                              <span className="text-zinc-500">{f.file}</span>
                              <span className="text-zinc-300">{f.message}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── TESTING ── */}
          {page === 'testing' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>Testing</SectionTitle>
              {!selected ? (
                <p className="text-sm text-zinc-500">Load a repository first.</p>
              ) : (
                <>
                  {/* Existing test result */}
                  {testResult && (
                    <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4">
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-2">Existing Tests (run during analysis)</div>
                      <div className="flex items-center gap-3">
                        <span className={testResult.status === 'passed' ? 'text-emerald-400' : testResult.status === 'skipped' ? 'text-zinc-500' : 'text-red-400'}>
                          {testResult.status === 'passed' ? '✓ PASSED' : testResult.status === 'skipped' ? '○ SKIPPED' : '✗ FAILED'}
                        </span>
                        <span className="text-xs text-zinc-400">{String(testResult.message ?? '')}</span>
                      </div>
                      {Boolean(testResult.command) && <div className="mt-2 font-mono text-xs text-zinc-500">{String(testResult.command)}</div>}
                    </div>
                  )}

                  {/* AI tests */}
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium text-zinc-200">AI-Generated Tests</div>
                      <div className="text-xs text-zinc-500 mt-0.5">
                        Generates route-level tests for FastAPI / Flask repositories and runs them via pytest.
                      </div>
                    </div>
                    <button
                      onClick={onRunAiTests}
                      disabled={runningAiTests}
                      className="rounded bg-sky-700 px-4 py-2 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50"
                    >
                      {runningAiTests ? 'Running…' : 'Run AI Tests'}
                    </button>
                  </div>

                  {aiTests && (
                    <div className="space-y-3">
                      <div className={`flex items-center gap-3 rounded border px-4 py-3 text-xs ${
                        aiTests.status === 'passed' ? 'border-emerald-700/50 bg-emerald-950/20' :
                        aiTests.status === 'skipped' ? 'border-zinc-700 bg-zinc-900/60' :
                        'border-red-700/50 bg-red-950/20'
                      }`}>
                        <span className={
                          aiTests.status === 'passed' ? 'text-emerald-400 font-bold' :
                          aiTests.status === 'skipped' ? 'text-zinc-500' : 'text-red-400 font-bold'
                        }>
                          {aiTests.status === 'passed' ? '✓ PASSED' : aiTests.status === 'skipped' ? '○ SKIPPED' : '✗ FAILED'}
                        </span>
                        <span className="text-zinc-300 font-medium">{aiTests.message}</span>
                        {aiTests.command && <span className="ml-auto text-zinc-500 font-mono text-[11px]">{aiTests.command}</span>}
                      </div>

                      <details className="rounded border border-zinc-800 px-3 py-2">
                        <summary className="cursor-pointer text-xs text-zinc-400">Technical test details</summary>
                        <div className="mt-3 space-y-4">

                      {/* Test Inferences */}
                      {aiTests.test_inferences && aiTests.test_inferences.length > 0 && (
                        <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4 space-y-3">
                          <div className="flex items-center justify-between">
                            <div className="text-[10px] uppercase tracking-widest text-sky-400 font-semibold">
                              What These Tests Infer About The Codebase
                            </div>
                            <span className="text-[10px] text-zinc-500 font-mono">
                              {aiTests.test_inferences.length} specs inferred
                            </span>
                          </div>
                          <div className="space-y-2">
                            {aiTests.test_inferences.map(inf => (
                              <div key={inf.name + inf.target} className="rounded border border-zinc-800 bg-zinc-950/60 p-2.5 text-xs">
                                <div className="flex items-center gap-2 mb-1">
                                  <span className="rounded bg-sky-950 border border-sky-800/60 px-1.5 py-0.5 text-[10px] font-mono text-sky-300">
                                    {inf.target}
                                  </span>
                                  <span className="font-medium text-zinc-200">{inf.name}</span>
                                </div>
                                <p className="text-zinc-400 text-[11px] leading-relaxed">{inf.inference}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* AI Failure Analysis & Suggested Remediation */}
                      {aiTests.failure_analysis && (
                        <div className="rounded border border-red-700/40 bg-red-950/20 p-4 space-y-3">
                          <div className="flex items-center justify-between">
                            <div className="text-[10px] uppercase tracking-widest text-red-400 font-semibold">
                              Failure Diagnostics &amp; Suggested Remediation
                            </div>
                            {Boolean(aiTests.failure_analysis.category) && (
                              <span className="rounded bg-red-900/60 border border-red-700/60 px-2 py-0.5 text-[10px] font-mono text-red-200">
                                {String(aiTests.failure_analysis.category)}
                              </span>
                            )}
                          </div>
                          <div className="space-y-2 text-xs">
                            <div>
                              <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">Likely Root Cause</div>
                              <div className="bg-red-950/40 p-2.5 rounded border border-red-900/30">
                                <FormattedText text={String(aiTests.failure_analysis.likely_cause ?? 'No analysis provided')} />
                              </div>
                            </div>
                            {Boolean(aiTests.failure_analysis.suggested_fix) && (
                              <div>
                                <div className="text-[10px] uppercase tracking-widest text-emerald-500 mb-1">Suggested Fix</div>
                                <div className="text-emerald-300 bg-emerald-950/30 p-2.5 rounded border border-emerald-900/40 leading-relaxed font-sans text-xs flex gap-2 items-start">
                                  <span className="text-emerald-400 font-bold shrink-0">→</span>
                                  <span>{String(aiTests.failure_analysis.suggested_fix)}</span>
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      )}

                      {aiTests.generated_tests.map(tc => (
                        <div key={tc.path} className="space-y-2">
                          <div className="text-xs font-medium text-zinc-300">{tc.name}</div>
                          <div className="text-xs text-zinc-500">{tc.rationale}</div>
                          <TerminalBlock text={tc.code} maxH="12rem" />
                        </div>
                      ))}

                      {(aiTests.stdout || aiTests.stderr) && (
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div>
                            <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-1">stdout</div>
                            <TerminalBlock text={aiTests.stdout || 'No output'} maxH="10rem" />
                          </div>
                          <div>
                            <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-1">stderr</div>
                            <TerminalBlock text={aiTests.stderr || 'No output'} maxH="10rem" />
                          </div>
                        </div>
                      )}
                        </div>
                      </details>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── DOCKER ── */}
          {page === 'docker' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>Docker</SectionTitle>
              {!selected || !analysis ? (
                <p className="text-sm text-zinc-500">Analyze a repository first.</p>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <Pill label="Dockerfile" value={analysis.has_dockerfile ? 'Present in repo' : 'Generated'} />
                    <Pill label="Application Port" value={String(extra.port ?? '—')} />
                    <Pill label="Framework" value={analysis.framework} />
                    <Pill label="Entry Point" value={analysis.entrypoint} />
                  </div>

                  {dockerResult && (
                    <div className="rounded border border-zinc-700 bg-zinc-900/60 p-4">
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-2">Docker Generation</div>
                      <div className="text-xs text-zinc-300">{dockerResult.message}</div>
                    </div>
                  )}

                  {files.find(f => f.filename === 'Dockerfile') && (
                    <>
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600">Dockerfile</div>
                      <TerminalBlock text={files.find(f => f.filename === 'Dockerfile')!.content} maxH="18rem" />
                    </>
                  )}

                  {/* Build status from deployments */}
                  {latestDeployment && (
                    <div className="rounded border border-zinc-700 bg-zinc-900/60 px-4 py-3 text-xs">
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mb-2">Last Build / Deploy</div>
                      <div className="flex gap-4">
                        <span className={latestDeployment.status === 'healthy' ? 'text-emerald-400' : latestDeployment.status === 'failed' ? 'text-red-400' : 'text-yellow-400'}>
                          {latestDeployment.status}
                        </span>
                        <span className="text-zinc-500">{latestDeployment.environment} / {latestDeployment.version}</span>
                        {latestDeploymentImage && <span className="font-mono text-zinc-400">{latestDeploymentImage}</span>}
                      </div>
                    </div>
                  )}

                  <button
                    onClick={() => { setPage('kubernetes'); onDeployStaging() }}
                    disabled={deploying || busy || files.length === 0}
                    className="rounded bg-sky-700 px-4 py-2 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50"
                  >
                    {deploying ? 'Building & Deploying…' : 'Build & Deploy to Staging'}
                  </button>
                </>
              )}
            </div>
          )}

          {/* ── KUBERNETES ── */}
          {page === 'kubernetes' && (
            <div className="space-y-6 max-w-3xl">
              <SectionTitle>Remote Deployment / K3s</SectionTitle>
              {!selected ? (
                <p className="text-sm text-zinc-500">Load a repository first.</p>
              ) : (
                <>
                  <div className="rounded border border-zinc-800 bg-zinc-900/50 p-4">
                    <div className="mb-3 text-[10px] uppercase tracking-widest text-zinc-500">Infrastructure</div>
                    <label className="mb-3 flex items-center gap-3 text-xs text-zinc-300">
                      <span>Deployment target</span>
                      <select
                        value={deploymentTarget}
                        onChange={event => setDeploymentTarget(event.target.value)}
                        className="rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs"
                      >
                        {(systemStatus?.deployment_targets ?? ['remote']).map(target => (
                          <option key={target} value={target}>{target === 'remote' ? 'AWS EC2 / K3s (Primary)' : 'Local kind (Development)'}</option>
                        ))}
                      </select>
                    </label>
                    <div className="grid gap-3 sm:grid-cols-3">
                      <Pill label="Deployment Target" value={deploymentTarget === 'remote' ? 'AWS EC2 / K3s' : 'Local kind'} />
                      <Pill label={deploymentTarget === 'remote' ? 'EC2 Worker' : 'Local kind'} value={deploymentTarget === 'remote' ? (systemStatus?.worker?.online ? 'Connected' : 'Offline') : (systemStatus?.local_kind?.ok ? 'Ready' : 'Unavailable')} />
                      <Pill label={deploymentTarget === 'remote' ? 'K3s Node' : 'kind Cluster'} value={deploymentTarget === 'remote' ? (systemStatus?.worker?.details?.node_status ? String(systemStatus.worker.details.node_status) : 'Waiting for worker health') : (systemStatus?.local_kind?.clusters?.join(', ') || 'No kind cluster')} />
                    </div>
                    <div className="mt-2 text-[11px] text-zinc-500">
                      {deploymentTarget === 'remote' ? (systemStatus?.worker?.message ?? 'Worker status unavailable') : (systemStatus?.local_kind?.message ?? 'Local kind status unavailable')}
                      {deploymentTarget === 'remote' && systemStatus?.worker?.last_seen && ` · Last heartbeat ${new Date(systemStatus.worker.last_seen).toLocaleString()}`}
                    </div>
                  </div>
                  {/* Pipeline steps for K8s */}
                  {(['Docker', 'Kubernetes', 'Staging'] as const).map(name => {
                    const step = pipelineMap[name]
                    const s = step?.status ?? 'pending'
                    return (
                      <div key={name} className="flex items-center gap-3 rounded border border-zinc-800 px-4 py-3 text-xs">
                        <span className={`font-bold text-base ${statusColor(s)}`}>{stepIcon(s)}</span>
                        <div className="flex-1">
                          <div className="flex justify-between">
                            <span className="text-zinc-200">{name}</span>
                            <span className={`rounded border px-1.5 py-0.5 text-[10px] ${statusBg(s)}`}>{s}</span>
                          </div>
                          {step?.result && <div className="mt-0.5 text-zinc-500">{step.result}</div>}
                          {step?.error && <div className="mt-0.5 text-red-400">{friendlyError(step.error)}</div>}
                        </div>
                      </div>
                    )
                  })}

                  {Boolean(analysis?.is_multiservice || analysis?.analysis_result?.is_multiservice) && (
                    <div className="rounded border border-sky-800/40 bg-sky-950/20 p-3 text-xs flex items-start gap-2.5">
                      <div className="shrink-0 mt-0.5 text-sky-400 font-bold">ℹ</div>
                      <div>
                        <div className="font-medium text-sky-300">Multi-Service Architecture Detected</div>
                        <p className="text-zinc-400 text-[11px] mt-0.5 leading-relaxed">
                          This project includes both backend and frontend components ({detectedServices}).
                          CodeDeck containerizes and deploys the core <strong>{analysis?.framework || 'backend'}</strong> service to Kubernetes.
                          The live API, health probes, and documentation are accessible below.
                        </p>
                      </div>
                    </div>
                  )}

                  {deploymentContract && (
                    <div className={`rounded border p-4 text-xs ${
                      deploymentContract.status === 'ready'
                        ? 'border-emerald-700/30 bg-emerald-950/20'
                        : deploymentContract.status === 'blocked'
                          ? 'border-red-700/30 bg-red-950/20'
                          : 'border-amber-700/30 bg-amber-950/20'
                    }`}>
                      <div className="flex items-center justify-between gap-3">
                        <div className="text-[10px] uppercase tracking-widest text-zinc-500">Deployment Contract</div>
                        <span className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${
                          deploymentContract.status === 'ready' ? 'border-emerald-500/30 text-emerald-400' :
                          deploymentContract.status === 'blocked' ? 'border-red-500/30 text-red-400' : 'border-amber-500/30 text-amber-400'
                        }`}>{deploymentContract.status}</span>
                      </div>
                      <p className="mt-2 text-zinc-300">{deploymentContract.summary}</p>
                      <div className="mt-3 grid gap-2 md:grid-cols-2">
                        {deploymentContract.checks?.map(check => (
                          <div key={check.name} className="rounded border border-zinc-800 bg-zinc-950/40 px-3 py-2">
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-medium text-zinc-200">{check.name}</span>
                              <span className={check.status === 'passed' ? 'text-emerald-400' : check.status === 'blocked' ? 'text-red-400' : 'text-amber-400'}>{check.status}</span>
                            </div>
                            <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">{check.message}</p>
                          </div>
                        ))}
                      </div>
                      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500">
                        <span>Target: {deploymentContract.target}</span>
                        {deploymentContract.entrypoint && <span>Entrypoint: {deploymentContract.entrypoint}</span>}
                        <span>Preview: {deploymentContract.preview_path}</span>
                      </div>
                    </div>
                  )}

                  {/* Deploy controls */}
                  <div className="flex flex-wrap gap-3 items-center">
                    <button
                      onClick={onDeployStaging}
                      disabled={deploying || cancelingStaging || busy || files.length === 0 || selected.status === 'deploying' || selected.status === 'staging_cancel_requested'}
                      className="rounded bg-sky-700 px-4 py-2 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50 transition-colors"
                    >
                      {deploying ? 'Deploying Staging…' : 'Deploy Staging'}
                    </button>
                    {stagingIsActive && (
                      <button
                        onClick={onCancelStaging}
                        disabled={cancelingStaging || selected.status === 'staging_cancel_requested'}
                        className="rounded border border-red-700/70 bg-red-950/30 px-4 py-2 text-xs font-medium text-red-300 hover:bg-red-900/40 disabled:opacity-50 transition-colors"
                      >
                        {cancelingStaging || selected.status === 'staging_cancel_requested' ? 'Canceling Staging…' : 'Cancel Staging'}
                      </button>
                    )}
                    {(deploymentTarget === 'local' && ['staging_healthy'].includes(selected.status)) && (
                      <a
                        href={`/api/projects/${selected.id}/preview/docs`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded bg-purple-700 px-4 py-2 text-xs font-medium text-white hover:bg-purple-600 flex items-center gap-1.5 shadow-lg shadow-purple-900/30 transition-colors"
                      >
                        <span>Open Live Preview (Swagger Docs)</span>
                        <span className="text-[10px] opacity-75">↗</span>
                      </a>
                    )}
                    {deploymentTarget === 'remote' && remoteApplicationUrl && selected.status === 'staging_healthy' && (
                      <a href={remoteApplicationUrl} target="_blank" rel="noopener noreferrer" className="rounded bg-purple-700 px-4 py-2 text-xs font-medium text-white hover:bg-purple-600">
                        Open Deployed Application
                      </a>
                    )}
                  </div>

                  {stagingIsActive && (
                    <div className="rounded border border-sky-800/50 bg-sky-950/20 p-4 text-xs">
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <span className="font-medium text-sky-200">{selected.status === 'staging_cancel_requested' ? 'Canceling staging deployment' : 'Remote staging deployment'}</span>
                        <span className="font-mono text-sky-400">{latestDeployment?.status ?? 'RUNNING'}</span>
                      </div>
                      <p className="mt-2 text-[11px] text-sky-300/80">{selected.status === 'staging_cancel_requested' && stagingPhase === 'Cleaning up staging resources' ? stagingPhase : selected.status === 'staging_cancel_requested' ? 'Stopping the active staging operation…' : stagingPhase}</p>
                    </div>
                  )}

                  {deployments.length > 0 && (
                    <div className="rounded border border-zinc-800 bg-zinc-900/50 p-4">
                      <div className="mb-3 text-[10px] uppercase tracking-widest text-zinc-500">Deployment History and Worker Logs</div>
                      <div className="space-y-3">
                        {deployments.map(deployment => {
                          const details = (deployment.details ?? {}) as {
                            target?: string; namespace?: string; image?: string; app_name?: string;
                            ingress_hostname?: string; application_port?: number; failure_reason?: string;
                            logs?: Array<{ time?: string; message?: string }>;
                            smoke?: { status?: string; url?: string | null; message?: string };
                            result?: { rollout?: string; deployment_status?: { ready_replicas?: number; replicas?: number } };
                          }
                          return (
                            <div key={deployment.id} className="rounded border border-zinc-800 p-3 text-xs">
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <span className="font-semibold text-zinc-200">#{deployment.id} · {deployment.status}</span>
                                <span className="text-zinc-500">{deployment.created_at ? new Date(deployment.created_at).toLocaleString() : ''}</span>
                              </div>
                              <div className="mt-2 grid gap-2 sm:grid-cols-2 text-zinc-400">
                                <span>Target: {details.target ?? deployment.deployment_type}</span>
                                <span>Namespace: {details.namespace ?? '—'}</span>
                                <span>Image: {details.image ?? '—'}</span>
                                <span>Deployment / Service: {details.app_name ?? '—'}</span>
                                <span>Application port: {details.application_port ?? '—'}</span>
                                <span>Pod readiness: {details.result?.deployment_status?.ready_replicas ?? 0}/{details.result?.deployment_status?.replicas ?? '—'}</span>
                                <span>Smoke test: {details.smoke?.status ?? 'pending'}</span>
                                <span>URL: {details.smoke?.url ?? 'No public hostname configured'}</span>
                              </div>
                              {details.failure_reason && <p className="mt-2 text-red-400">{details.failure_reason}</p>}
                              {deployment.deployment_type === 'remote_staging' && deployment.status === 'SUCCEEDED' && (
                                <button
                                  onClick={async () => {
                                    try {
                                      await cleanupDeployment(selected.id, deployment.id)
                                      await loadProject(selected)
                                    } catch (err) {
                                      setError(err instanceof Error ? err.message : 'Cleanup could not be queued')
                                    }
                                  }}
                                  className="mt-3 rounded border border-red-800/70 px-3 py-1.5 text-[11px] text-red-300 hover:bg-red-950/30"
                                >Queue namespace cleanup</button>
                              )}
                              {details.logs?.length ? (
                                <TerminalBlock text={details.logs.map(line => `${line.time ?? ''} ${line.message ?? ''}`.trim()).join('\n')} maxH="10rem" />
                              ) : null}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {autoscalingDeploymentId != null && autoscalingStatus && (
                    <section className="rounded border border-zinc-800 bg-zinc-900/50 p-4">
                      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                        <div><div className="text-[10px] uppercase tracking-widest text-zinc-500">Horizontal Pod Autoscaling</div><div className="mt-1 font-mono text-xs text-zinc-300">{autoscalingStatus.namespace}/{autoscalingStatus.deployment_name}</div></div>
                        <span className={`rounded border px-2 py-1 text-[10px] uppercase ${autoscalingStatus.hpa ? 'border-emerald-900 text-emerald-300' : 'border-zinc-700 text-zinc-400'}`}>{autoscalingStatus.hpa ? 'Active' : 'Not configured'}</span>
                      </div>
                      <p className="mb-3 text-[10px] text-zinc-500">HPA adjusts replicas within the configured range and owns the workload replica count while enabled. Removing it restores the saved replica count. CPU and memory targets require Metrics Server and container resource requests.</p>
                      {autoscalingError && <p className="mb-3 rounded border border-red-900/60 p-2 text-xs text-red-300">{autoscalingError}</p>}
                      {autoscalingStatus.monitoring_status !== 'available' && <p className="mb-3 rounded border border-amber-900/60 p-2 text-xs text-amber-300">{autoscalingStatus.message ?? 'Kubernetes monitoring is unavailable; do not treat the last sample as current.'}</p>}
                      {autoscalingStatus.operation && <p className={`mb-3 rounded border p-2 text-xs ${autoscalingStatus.operation.status === 'FAILED' ? 'border-red-900/60 text-red-300' : autoscalingStatus.operation.status === 'QUEUED' || ['RUNNING', 'BUILDING', 'DEPLOYING', 'VERIFYING'].includes(autoscalingStatus.operation.status) ? 'border-sky-900/60 text-sky-300' : 'border-zinc-800 text-zinc-400'}`}>Operation {autoscalingStatus.operation.status}: {autoscalingStatus.operation.message ?? 'No operation detail available.'}</p>}
                      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                        <label className="text-[10px] text-zinc-500">Minimum replicas<input type="number" min="1" max="8" value={autoscalingConfig.min_replicas} onChange={event => setAutoscalingConfig(config => ({ ...config, min_replicas: Number(event.target.value) }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                        <label className="text-[10px] text-zinc-500">Maximum replicas<input type="number" min="1" max="8" value={autoscalingConfig.max_replicas} onChange={event => setAutoscalingConfig(config => ({ ...config, max_replicas: Number(event.target.value) }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                        <label className="text-[10px] text-zinc-500">Target CPU utilization (%)<input type="number" min="10" max="95" value={autoscalingConfig.target_cpu_utilization} onChange={event => setAutoscalingConfig(config => ({ ...config, target_cpu_utilization: Number(event.target.value) }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                        <label className="text-[10px] text-zinc-500">Target memory utilization (%) · optional<input type="number" min="10" max="95" value={autoscalingConfig.target_memory_utilization ?? ''} onChange={event => setAutoscalingConfig(config => ({ ...config, target_memory_utilization: event.target.value === '' ? null : Number(event.target.value) }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                        <label className="text-[10px] text-zinc-500">Scale-up stabilization (seconds)<input type="number" min="0" max="3600" value={autoscalingConfig.behavior.scale_up_stabilization_seconds} onChange={event => setAutoscalingConfig(config => ({ ...config, behavior: { ...config.behavior, scale_up_stabilization_seconds: Number(event.target.value) } }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                        <label className="text-[10px] text-zinc-500">Scale-down stabilization (seconds)<input type="number" min="0" max="3600" value={autoscalingConfig.behavior.scale_down_stabilization_seconds} onChange={event => setAutoscalingConfig(config => ({ ...config, behavior: { ...config.behavior, scale_down_stabilization_seconds: Number(event.target.value) } }))} className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-200" /></label>
                      </div>
                      <div className="mb-4 flex flex-wrap gap-2">
                        <button disabled={autoscalingBusy} onClick={async () => {
                          if (!selected || autoscalingDeploymentId == null) return
                          if (autoscalingConfig.min_replicas > autoscalingConfig.max_replicas) { setAutoscalingError('Minimum replicas cannot exceed maximum replicas.'); return }
                          if (!window.confirm('Enable Kubernetes autoscaling for this deployment? Kubernetes will control replica count within your configured range. Metrics Server and CPU resource requests are required; memory scaling also requires memory requests.')) return
                          setAutoscalingBusy(true); setAutoscalingError(null)
                          try {
                            const result = await configureAutoscaling(selected.id, autoscalingDeploymentId, autoscalingConfig)
                            setAutoscalingStatus(await getAutoscaling(selected.id, autoscalingDeploymentId))
                            if (result.status === 'QUEUED') setAutoscalingStatus(previous => previous ? { ...previous, operation: { job_id: result.job_id, status: result.status, message: result.message } } : previous)
                          } catch (err) { setAutoscalingError(err instanceof Error ? err.message : 'Autoscaling configuration failed') }
                          finally { setAutoscalingBusy(false) }
                        }} className="rounded bg-sky-700 px-3 py-2 text-[11px] font-medium text-white hover:bg-sky-600 disabled:opacity-50">{autoscalingBusy ? 'Applying…' : autoscalingStatus.hpa ? 'Update autoscaling' : 'Enable autoscaling'}</button>
                        {autoscalingStatus.hpa && <button disabled={autoscalingBusy} onClick={async () => {
                          if (!selected || autoscalingDeploymentId == null || !window.confirm('Remove the CodeDeck-managed HPA? Kubernetes will stop adjusting replicas and CodeDeck will restore the replica count saved before autoscaling was enabled.')) return
                          setAutoscalingBusy(true); setAutoscalingError(null)
                          try {
                            const result = await disableAutoscaling(selected.id, autoscalingDeploymentId)
                            setAutoscalingStatus(await getAutoscaling(selected.id, autoscalingDeploymentId))
                            if (result.status === 'QUEUED') setAutoscalingStatus(previous => previous ? { ...previous, operation: { job_id: result.job_id, status: result.status, message: result.message } } : previous)
                          } catch (err) { setAutoscalingError(err instanceof Error ? err.message : 'Autoscaling cleanup failed') }
                          finally { setAutoscalingBusy(false) }
                        }} className="rounded border border-red-900 px-3 py-2 text-[11px] text-red-300 hover:bg-red-950/30 disabled:opacity-50">{autoscalingBusy ? 'Working…' : 'Disable autoscaling'}</button>}
                        <span className="self-center text-[9px] text-zinc-600">Last Kubernetes sample: {formatWorkflowTime(autoscalingStatus.sampled_at)}</span>
                      </div>
                      {autoscalingStatus.hpa ? <>
                        <div className="mb-3 grid gap-2 sm:grid-cols-4">
                          <div className="rounded border border-zinc-800 p-2 text-[10px] text-zinc-400">Replicas <strong className="text-zinc-200">{autoscalingStatus.hpa.current_replicas ?? 'Unavailable'}</strong> current · <strong className="text-zinc-200">{autoscalingStatus.hpa.desired_replicas ?? 'Unavailable'}</strong> desired</div>
                          <div className="rounded border border-zinc-800 p-2 text-[10px] text-zinc-400">Ready replicas <strong className="text-zinc-200">{autoscalingStatus.rollout ? `${autoscalingStatus.rollout.ready_replicas}/${autoscalingStatus.rollout.desired_replicas}` : 'Unavailable'}</strong></div>
                          <div className="rounded border border-zinc-800 p-2 text-[10px] text-zinc-400">Range <strong className="text-zinc-200">{autoscalingStatus.hpa.min_replicas}–{autoscalingStatus.hpa.max_replicas}</strong></div>
                          {(['cpu', 'memory'] as const).map(resource => {
                            const current = autoscalingStatus.hpa?.current_metrics?.find(metric => metric.name === resource)?.average_utilization
                            const target = autoscalingStatus.hpa?.targets?.find(metric => metric.name === resource)?.average_utilization
                            return <div key={resource} className="rounded border border-zinc-800 p-2 text-[10px] text-zinc-400">{resource.toUpperCase()} utilization <strong className="text-zinc-200">{current == null ? 'Unavailable' : `${current}%`}</strong> / target {target == null ? '—' : `${target}%`}</div>
                          })}
                        </div>
                        <div className="mb-4 text-[10px] text-zinc-500">Autoscaler state: {autoscalingStatus.hpa.conditions?.filter(condition => condition.status === 'False').map(condition => `${condition.type}: ${condition.message ?? condition.reason ?? 'not ready'}`).join(' · ') || 'Active; no unhealthy HPA conditions reported.'}</div>
                      </> : <div className="mb-4 rounded border border-zinc-800 p-3 text-[10px] text-zinc-500">{autoscalingStatus.metrics.status === 'unavailable' ? autoscalingStatus.metrics.message : 'No CodeDeck autoscaler is attached to this deployment.'}</div>}
                      {autoscalingStatus.hpa && autoscalingStatus.deployment_name.endsWith('-staging') && <details className="mb-4 rounded border border-zinc-800 px-3 py-2 text-[10px] text-zinc-400"><summary className="cursor-pointer text-zinc-300">Controlled staging demonstration</summary><ol className="mt-2 list-decimal space-y-1 pl-4"><li>Record the actual current and desired replica counts above.</li><li>Send a short, rate-limited load only to a test CPU-intensive route on this isolated staging deployment.</li><li>Watch actual CPU utilization against the configured target; HPA reconciliation is not immediate.</li><li>When sustained utilization crosses the target, observe Kubernetes increase desired replicas.</li><li>Watch current and ready replicas until the additional pods become ready.</li><li>Stop the load and observe the actual replica samples through the configured scale-down stabilization window.</li><li>Use a non-production endpoint only. CodeDeck does not generate traffic automatically.</li></ol><p className="mt-2 text-zinc-600">A bounded example and prerequisites are in docs/kubernetes-autoscaling.md.</p></details>}
                      {autoscalingStatus.events.length > 0 && <div><div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-500">Replica history · observed Kubernetes samples</div><div className="flex h-24 items-end gap-2 overflow-x-auto rounded border border-zinc-800 px-3 pt-2">{autoscalingStatus.events.map(event => {
                        const max = Math.max(1, ...autoscalingStatus.events.map(sample => sample.current_replicas ?? 0), ...autoscalingStatus.events.map(sample => sample.desired_replicas ?? 0))
                        return <div key={event.id} title={`${formatWorkflowTime(event.occurred_at)} · current ${event.current_replicas ?? 'unavailable'}, desired ${event.desired_replicas ?? 'unavailable'}`} className="flex h-full min-w-8 flex-col items-center justify-end gap-1"><div className="w-5 rounded-t bg-sky-500/70" style={{ height: event.desired_replicas == null && event.current_replicas == null ? '0%' : `${Math.max(8, ((event.desired_replicas ?? event.current_replicas ?? 0) / max) * 75)}%` }} /><span className="pb-1 text-[8px] text-zinc-600">{event.current_replicas ?? '—'}</span></div>
                      })}</div><div className="mt-1 flex justify-between text-[9px] text-zinc-600"><span>{formatWorkflowTime(autoscalingStatus.events[0].occurred_at)}</span><span>{formatWorkflowTime(autoscalingStatus.events[autoscalingStatus.events.length - 1].occurred_at)}</span></div></div>}
                    </section>
                  )}

                  {/* Smoke test result & Live Endpoints */}
                  {smokeResult && (
                    <div className={`rounded border p-4 text-xs ${
                      smokeResult.status === 'passed'
                        ? 'border-emerald-700/30 bg-emerald-950/20'
                        : 'border-red-700/30 bg-red-950/20'
                    }`}>
                      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Smoke Test & Live Endpoints</div>
                      <div className={`font-bold flex items-center gap-1.5 ${smokeResult.status === 'passed' ? 'text-emerald-400' : 'text-red-400'}`}>
                        <span>{smokeResult.status === 'passed' ? 'PASSED' : 'FAILED'}</span>
                        <span className="font-normal text-zinc-500">·</span>
                        <span className="font-normal text-zinc-400">{String(smokeResult.message ?? '')}</span>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <a
                          href={`/api/projects/${selected.id}/preview/docs`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1.5 rounded bg-emerald-600/20 px-3 py-1.5 text-xs text-emerald-300 hover:bg-emerald-600/30 border border-emerald-500/30 font-medium transition-colors"
                        >
                          <span>Interactive API Docs</span>
                          <span className="text-[10px] opacity-70">↗</span>
                        </a>
                        <a
                          href={`/api/projects/${selected.id}/preview/openapi.json`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1.5 rounded bg-zinc-800 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-700 border border-zinc-700 transition-colors"
                        >
                          <span>OpenAPI Schema</span>
                          <span className="text-[10px] opacity-70">↗</span>
                        </a>
                        <a
                          href={`/api/projects/${selected.id}/preview/`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1.5 rounded bg-zinc-800 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-700 border border-zinc-700 transition-colors"
                        >
                          <span>App Root Endpoint</span>
                          <span className="text-[10px] opacity-70">↗</span>
                        </a>
                      </div>
                    </div>
                  )}

                  {/* Deployment history */}
                  {deployments.length > 0 && (
                    <>
                      <div className="text-[10px] uppercase tracking-widest text-zinc-600 mt-2">Deployment History</div>
                      <div className="space-y-1.5">
                        {deployments.map(d => (
                          <div key={d.id} className="flex items-center gap-3 rounded border border-zinc-800 px-3 py-2 text-xs">
                            <span className="text-zinc-300 capitalize">{d.environment}</span>
                            <span className="text-zinc-500">{d.deployment_type.replaceAll('_', ' ')}</span>
                            <span className="text-zinc-400 capitalize">{d.environment}</span>
                            <span className={`ml-auto font-semibold ${
                              ['healthy', 'succeeded', 'SUCCEEDED'].includes(d.status) ? 'text-emerald-400' :
                              ['failed', 'FAILED'].includes(d.status) ? 'text-red-400' : 'text-zinc-400'
                            }`}>{d.status}</span>
                            <span className="text-zinc-600">{new Date(d.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {/* Failure analysis */}
                  {failureAnalysis && (
                    <div className="rounded border border-orange-700/30 bg-orange-950/20 p-4 text-xs">
                      <div className="text-[10px] uppercase tracking-widest text-orange-500 mb-2">Failure Analysis</div>
                      {failureAnalysis.repository_status === 'incompatible_or_faulty' && (
                        <div className="mb-2 rounded border border-red-700/40 bg-red-950/30 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-red-300">
                          Repository incompatible or incomplete
                        </div>
                      )}
                      <FormattedText text={String(failureAnalysis.likely_cause ?? 'No analysis')} />
                      {Boolean(failureAnalysis.suggested_fix) && (
                        <p className="mt-2 text-orange-300/70">→ {String(failureAnalysis.suggested_fix)}</p>
                      )}
                      {Boolean(failureAnalysis.details) && (
                        <details className="mt-3 rounded border border-zinc-800 bg-zinc-950/70 p-2">
                          <summary className="cursor-pointer text-zinc-400">Show staging error details</summary>
                          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-zinc-400">{String(failureAnalysis.details)}</pre>
                        </details>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── LOGS ── */}
          {page === 'logs' && (
            <div className="space-y-4 max-w-4xl">
              <SectionTitle>Activity Logs</SectionTitle>
              <div className="flex justify-between items-center">
                <div className="text-xs text-zinc-500">{logs.length} entries</div>
                <button onClick={() => setLogs([])} className="text-xs text-zinc-600 hover:text-zinc-400">Clear</button>
              </div>
              <div className="overflow-y-auto rounded border border-zinc-800 bg-zinc-950" style={{ maxHeight: 'calc(100vh - 14rem)' }}>
                {logs.length === 0 ? (
                  <div className="p-6 text-center text-xs text-zinc-700">No log entries yet. Run analysis or deployment.</div>
                ) : (
                  <div className="divide-y divide-zinc-900">
                    {logs.map((entry, i) => (
                      <div key={i} className="flex gap-3 px-4 py-2 text-xs hover:bg-zinc-900/40">
                        <span className="shrink-0 text-zinc-700 tabular-nums">{entry.ts}</span>
                        <span className={`shrink-0 w-20 font-semibold ${
                          entry.tag === 'Analysis' ? 'text-sky-400' :
                          entry.tag === 'Q&A' ? 'text-purple-400' :
                          entry.tag === 'Testing' ? 'text-yellow-400' :
                          entry.tag.startsWith('Docker') ? 'text-orange-400' :
                          'text-zinc-400'
                        }`}>[{entry.tag}]</span>
                        <span className={entry.text.startsWith('ERROR') ? 'text-red-300' : 'text-zinc-300'}>{entry.text}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* ── SETTINGS ── */}
          {page === 'settings' && (
            <div className="space-y-6 max-w-xl">
              <SectionTitle>System Status</SectionTitle>
              {!systemStatus ? (
                <p className="text-sm text-zinc-500">Loading system status…</p>
              ) : (
                <div className="space-y-2">
                  {([
                    ['Backend API', systemStatus.backend],
                    ['Database', systemStatus.database],
                    ['Docker', systemStatus.docker],
                    ['kind', systemStatus.kind],
                    ['kubectl', systemStatus.kubectl],
                  ] as [string, { ok: boolean; message: string }][]).map(([name, stat]) => (
                    <div key={name} className="flex items-center justify-between rounded border border-zinc-800 px-4 py-3 text-xs">
                      <span className="text-zinc-400">{name}</span>
                      <div className="flex items-center gap-2">
                        <span className="text-zinc-500 truncate max-w-xs text-right">{stat.message}</span>
                        <span className={stat.ok ? 'text-emerald-400 font-bold' : 'text-red-400 font-bold'}>
                          {stat.ok ? '✓' : '✗'}
                        </span>
                      </div>
                    </div>
                  ))}

                  {systemStatus.kind.clusters.length > 0 && (
                    <div className="rounded border border-zinc-800 px-4 py-3 text-xs">
                      <span className="text-zinc-600">kind clusters: </span>
                      <span className="text-zinc-300">{systemStatus.kind.clusters.join(', ')}</span>
                    </div>
                  )}
                </div>
              )}

              <button
                onClick={() => getSystemStatus().then(setSystemStatus).catch(() => undefined)}
                className="rounded border border-zinc-700 px-4 py-2 text-xs text-zinc-400 hover:bg-zinc-800"
              >
                Refresh Status
              </button>

              <SectionTitle>AI &amp; RAG Engine</SectionTitle>
              <div className="space-y-3 rounded border border-zinc-800 bg-zinc-900/60 p-4 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-zinc-400">Active LLM Provider</span>
                  <span className="font-mono text-zinc-200 font-semibold">
                    {systemStatus?.ai_provider?.provider ?? 'Grounded Heuristic'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-zinc-400">Active Model</span>
                  <span className="font-mono text-zinc-400">
                    {systemStatus?.ai_provider?.model ?? 'rule-based-engine'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-zinc-400">Answer Mode</span>
                  <span className={systemStatus?.ai_provider?.status === 'configured' ? 'font-mono text-emerald-400' : 'font-mono text-amber-400'}>
                    {systemStatus?.ai_provider?.status === 'configured' ? 'LLM + grounded RAG' : 'Grounded heuristic fallback'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-zinc-400">RAG Vector Index</span>
                  <span className="font-mono text-zinc-400">Local semantic vectors + BM25 · RRF fusion</span>
                </div>
                {(['langfuse', 'prometheus'] as const).map(name => {
                  const status = systemStatus?.integrations?.[name]
                  return <div key={name} className="flex items-center justify-between gap-3">
                    <span className="text-zinc-400">{name === 'langfuse' ? 'Langfuse tracing' : 'Prometheus metrics'}</span>
                    <span className={`font-mono ${status?.status === 'available' ? 'text-emerald-400' : status?.status === 'disabled' ? 'text-zinc-500' : 'text-amber-400'}`}>
                      {status?.status ?? 'unavailable'}
                    </span>
                  </div>
                })}
                <div className="pt-2 border-t border-zinc-800/80 text-zinc-500 leading-relaxed text-[11px]">
                  {systemStatus?.ai_provider?.message ?? 'DeployMind prioritizes OpenAI, then Groq or xAI depending on the configured key. Every answer remains grounded in retrieved repository files; heuristics are used only when no provider is configured or all providers are unavailable.'}
                </div>
              </div>

              <SectionTitle>About CodeDeck</SectionTitle>
              <div className="space-y-2 rounded border border-zinc-800 bg-zinc-900/60 p-4 text-xs text-zinc-400">
                <div><span className="text-zinc-300">Version:</span> 1.0.0</div>
                <div><span className="text-zinc-300">Stack:</span> FastAPI · PostgreSQL · React · Vite · kind</div>
                <div><span className="text-zinc-300">AI:</span> OpenAI GPT · Local embeddings · RAG pipeline</div>
                <div className="pt-2 border-t border-zinc-800 text-zinc-600">
                  Workflow: GitHub → Analysis → RAG → Q&amp;A → AI Testing → Docker → Kubernetes → Smoke Test
                </div>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
