import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import type { Job, JobBucket } from '../types'
import {
  buckets,
  matchesBucket,
  matchesScore,
  tailoringActive,
  validBucket,
  validTab,
} from './jobViewState'

const emptyJobs: Job[] = []

export function useJobsWorkspace() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [pipelineRunId, setPipelineRunId] = useState<string | null>(() => {
    const current = sessionStorage.getItem('rolesailPipelineRun')
    const legacy = sessionStorage.getItem('applypilotPipelineRun')
    if (!current && legacy) sessionStorage.setItem('rolesailPipelineRun', legacy)
    return current ?? legacy
  })
  const jobsQuery = useQuery({ queryKey: ['jobs'], queryFn: api.jobs, staleTime: 3_000 })
  const tailoringQuery = useQuery({
    queryKey: ['tailoring-status'],
    queryFn: api.tailoringStatus,
    refetchInterval: ({ state }) => tailoringActive(state.data) ? 1_000 : false,
  })
  const discoveryQuery = useQuery({
    queryKey: ['discovery-status'],
    queryFn: api.discoveryStatus,
    refetchInterval: ({ state }) => state.data?.status === 'running' ? 1_000 : false,
  })
  const pipelineQuery = useQuery({
    queryKey: ['pipeline-status', pipelineRunId],
    queryFn: () => api.pipelineStatus(pipelineRunId),
    refetchInterval: ({ state }) => state.data?.run?.status === 'running' ? 1_000 : false,
  })
  const previousActivity = useRef('')
  const activity = `${tailoringQuery.data?.status}:${discoveryQuery.data?.status}:${pipelineQuery.data?.run?.status}`

  useEffect(() => {
    const previous = previousActivity.current
    previousActivity.current = activity
    if (previous && previous !== activity && (
      tailoringQuery.data?.status === 'idle'
      || ['complete', 'error'].includes(discoveryQuery.data?.status ?? '')
      || (pipelineQuery.data?.run && pipelineQuery.data.run.status !== 'running')
    )) void queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }, [activity, discoveryQuery.data?.status, pipelineQuery.data?.run, queryClient, tailoringQuery.data?.status])

  const jobs = jobsQuery.data?.jobs ?? emptyJobs
  const bucket = validBucket(params.get('bucket'))
  const score = params.get('score') ?? 'all'
  const search = params.get('q') ?? ''
  const tab = validTab(params.get('tab'))
  const counts = useMemo(() => Object.fromEntries(
    buckets.map(({ key }) => [key, jobs.filter((job) => matchesBucket(job, key) && matchesScore(job, score)).length]),
  ) as Record<JobBucket, number>, [jobs, score])
  const companies = useMemo(() => {
    const countsByCompany = new Map<string, number>()
    jobs.filter((job) => matchesBucket(job, bucket) && matchesScore(job, score))
      .forEach((job) => countsByCompany.set(job.company, (countsByCompany.get(job.company) ?? 0) + 1))
    return [...countsByCompany.entries()].sort(([left], [right]) => left.localeCompare(right))
  }, [bucket, jobs, score])
  const companyParam = `company_${bucket}`
  const selectedCompanies = useMemo(() => params.has(companyParam)
    ? params.getAll(companyParam).filter(Boolean)
    : companies.map(([company]) => company), [companies, companyParam, params])
  const filteredJobs = useMemo(() => jobs.filter((job) => {
    const haystack = `${job.title} ${job.company} ${job.location}`.toLowerCase()
    return matchesBucket(job, bucket)
      && matchesScore(job, score)
      && (!search || haystack.includes(search.toLowerCase()))
      && selectedCompanies.includes(job.company)
  }), [bucket, jobs, score, search, selectedCompanies])
  const selectedUrl = params.get('job')
  const selectedJob = filteredJobs.find((job) => job.url === selectedUrl) ?? filteredJobs[0] ?? null

  useEffect(() => {
    if (selectedJob && selectedJob.url !== selectedUrl) {
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.set('job', selectedJob.url)
        return next
      }, { replace: true })
    } else if (!selectedJob && selectedUrl) {
      setParams((current) => {
        const next = new URLSearchParams(current)
        next.delete('job')
        return next
      }, { replace: true })
    }
  }, [selectedJob, selectedUrl, setParams])

  const updateParam = (key: string, value?: string) => setParams((current) => {
    const next = new URLSearchParams(current)
    if (value && value !== 'all' && !(key === 'bucket' && value === 'jobs')) next.set(key, value)
    else next.delete(key)
    if (key !== 'job' && !['tab', 'q'].includes(key)) next.delete('job')
    return next
  })

  const setCompanySelection = (values: string[]) => setParams((current) => {
    const next = new URLSearchParams(current)
    next.delete(companyParam)
    if (values.length !== companies.length || !companies.every(([company]) => values.includes(company))) {
      if (values.length) values.forEach((value) => next.append(companyParam, value))
      else next.append(companyParam, '')
    }
    next.delete('job')
    return next
  })
  const chooseCompany = (company: string, checked: boolean) => {
    const selections = new Set(selectedCompanies)
    if (checked) selections.add(company)
    else selections.delete(company)
    setCompanySelection([...selections])
  }

  const startPipeline = useMutation({
    mutationFn: api.startPipeline,
    onSuccess: (result) => {
      setPipelineRunId(result.id)
      sessionStorage.setItem('rolesailPipelineRun', result.id)
      void queryClient.invalidateQueries({ queryKey: ['pipeline-status'] })
    },
  })
  const startDiscovery = useMutation({ mutationFn: api.startDiscovery, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['discovery-status'] }) })
  const startBulkTailoring = useMutation({ mutationFn: api.startBulkTailoring, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['tailoring-status'] }) })

  return {
    jobs, filteredJobs, selectedJob, bucket, counts, score, search, tab,
    companies, selectedCompanies, updateParam, chooseCompany, setCompanySelection,
    tailoringQuery, discoveryQuery, pipelineQuery,
    startPipeline, startDiscovery, startBulkTailoring,
  }
}
