import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AdvancedPage from './AdvancedPage'
import { api } from '../lib/api'

// Focused coverage for AdvancedPage's `uploadNew()` upload-and-poll flow only
// -- the rest of the page (org switching, JSON editing, other tabs) has no
// test coverage and is out of scope here.
vi.mock('../lib/api', () => ({
  api: {
    listOrgs: vi.fn(),
    listConfig: vi.fn(),
    uploadKnowledgeBaseFiles: vi.fn(),
    knowledgeBaseUploadJob: vi.fn(),
    putConfigItem: vi.fn(),
    skillVersions: vi.fn(),
    skillReferences: vi.fn(),
  },
}))

const mockedApi = vi.mocked(api)

const renderPage = () => render(<AdvancedPage />)

// Drives the page to the "Upload files" form on the Knowledge bases tab,
// with a name and one file already filled in, ready for "Create from files".
const setUpUploadForm = async () => {
  renderPage()
  // Wait for the orgs to have LANDED, not merely for listOrgs to have been
  // called. Those are different moments, and clicking the tab in between is
  // what made this flaky on CI: the load effect bails out while `org` is
  // still null (knowledge_bases is orgScope 'required'), so the listConfig
  // below only happens on the effect's later re-run, once listOrgs resolves
  // and sets the org. That whole chain -- promise, setState, re-render,
  // effect -- has to fit inside waitFor's 1s budget, and on a loaded 2-core
  // runner it did not. Waiting here instead means the tab click issues the
  // call directly, in one render cycle.
  await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))

  fireEvent.click(screen.getByText('Knowledge bases'))
  await waitFor(() => expect(mockedApi.listConfig).toHaveBeenCalledWith('knowledge_bases', 'acme'))

  fireEvent.click(screen.getByText('Upload files'))
  fireEvent.change(screen.getByPlaceholderText('Knowledge base name'), { target: { value: 'policies' } })
  const file = new File(['refunds within 30 days'], 'doc.txt', { type: 'text/plain' })
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(fileInput, { target: { files: [file] } })
}

describe('AdvancedPage upload flow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listOrgs.mockResolvedValue([{ name: 'acme', display_name: 'Acme', active: true }])
    mockedApi.listConfig.mockResolvedValue([])
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('polls the ingestion job and shows a success message with the polled file/chunk counts, then populates the JSON editor from the job config', async () => {
    mockedApi.uploadKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.knowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1,
      status: 'completed',
      file_count: 1,
      documents_succeeded: 1,
      documents_failed: 0,
      chunk_count: 2,
      errors: [],
      config: { type: 'local_folder', chunk_size: 500 },
    })

    await setUpUploadForm()
    fireEvent.click(screen.getByText('Create from files'))

    await waitFor(() => expect(mockedApi.knowledgeBaseUploadJob).toHaveBeenCalledWith('policies', 1, 'acme'))

    // Message text reads the polled job's counts, not the immediate upload response.
    await screen.findByText("Created 'policies' — 1 file(s), 2 chunk(s) indexed.")

    // JSON editor is populated from the completed job's config.
    const textarea = document.querySelector('textarea') as HTMLTextAreaElement
    expect(textarea.value).toBe(JSON.stringify({ type: 'local_folder', chunk_size: 500 }, null, 2))

    // The newly created item is selected.
    expect(screen.getByRole('heading', { name: 'policies' })).toBeInTheDocument()
  })

  it('stops polling after the cap and reports "still processing" rather than success or failure', async () => {
    mockedApi.uploadKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.knowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1,
      status: 'running',
      file_count: 1,
      documents_succeeded: 0,
      documents_failed: 0,
      chunk_count: 0,
      errors: [],
      config: null,
    })

    await setUpUploadForm()

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(screen.getByText('Create from files'))
      // Well past the cap (1 immediate check + 120 x 500ms).
      await vi.advanceTimersByTimeAsync(120 * 500 + 5000)
    })
    vi.useRealTimers()

    expect(mockedApi.knowledgeBaseUploadJob).toHaveBeenCalledTimes(121)
    expect(screen.queryByText(/Created 'policies'/)).not.toBeInTheDocument()
    expect(screen.getByText(/still being processed/i)).toBeInTheDocument()
    expect(screen.getByText('Create from files')).toBeInTheDocument()
  })

  it('does not crash and does not report success when the ingestion job fails', async () => {
    mockedApi.uploadKnowledgeBaseFiles.mockResolvedValue({ name: 'policies', job_id: 1, status: 'queued' })
    mockedApi.knowledgeBaseUploadJob.mockResolvedValue({
      job_id: 1,
      status: 'failed',
      file_count: 1,
      documents_succeeded: 0,
      documents_failed: 1,
      chunk_count: 0,
      errors: [{ filename: 'doc.txt', error: 'could not parse' }],
      config: null,
    })

    await setUpUploadForm()
    fireEvent.click(screen.getByText('Create from files'))

    await waitFor(() => expect(mockedApi.knowledgeBaseUploadJob).toHaveBeenCalledWith('policies', 1, 'acme'))

    // The upload button returns to its idle label rather than hanging or crashing.
    await screen.findByText('Create from files')

    // The catch (e) { setError(...) } path was taken, not the success path:
    // no success message, and the name field was not cleared (only the
    // success branch resets it).
    expect(screen.queryByText(/Created 'policies'/)).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText('Knowledge base name')).toHaveValue('policies')

    // The page itself is still intact -- no crash, no unhandled rejection.
    expect(screen.getByText('Advanced configuration')).toBeInTheDocument()
  })
})

describe('AdvancedPage item filter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listOrgs.mockResolvedValue([{ name: 'acme', display_name: 'Acme', active: true }])
    mockedApi.listConfig.mockResolvedValue([
      { name: 'refund_policy' },
      { name: 'delivery_policy' },
      { name: 'staff_handbook' },
    ])
  })

  // An org with dozens of collections had no way to find one but to scan the
  // list by eye (audit finding F15).
  it('narrows the list to matching names, and says so when nothing matches', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))
    await screen.findByText('refund_policy')

    fireEvent.change(screen.getByPlaceholderText('Filter…'), { target: { value: 'policy' } })

    expect(screen.getByText('refund_policy')).toBeInTheDocument()
    expect(screen.getByText('delivery_policy')).toBeInTheDocument()
    expect(screen.queryByText('staff_handbook')).not.toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('Filter…'), { target: { value: 'zzz' } })
    expect(screen.getByText('Nothing matches that filter.')).toBeInTheDocument()
  })

  // Filtering is display-only: it must never change which org a mutation
  // targets, so switching tabs clears it rather than carrying it across.
  it('clears the filter when the tab changes', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))
    await screen.findByText('refund_policy')

    fireEvent.change(screen.getByPlaceholderText('Filter…'), { target: { value: 'policy' } })
    fireEvent.click(screen.getByText('Skills'))

    await waitFor(() => expect(screen.getByPlaceholderText('Filter…')).toHaveValue(''))
  })
})


describe('AdvancedPage skills tab: locked built-ins, version history, references', () => {
  const builtinRow = {
    name: 'email_triage_reply',
    org: null,
    config: { name: 'email_triage_reply', instructions: 'triage', tools: [] },
    version: 2,
    builtin: true,
  }
  const versionRows = [
    { version: 2, config: { instructions: 'triage v2' }, created_by: null, created_at: '2026-09-01T00:00:00Z', current: true },
    { version: 1, config: { instructions: 'triage v1' }, created_by: 'admin', created_at: '2026-08-01T00:00:00Z', current: false },
  ]

  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listOrgs.mockResolvedValue([
      { name: 'acme', display_name: 'Acme', active: true },
      { name: 'ghost', display_name: 'Ghost', active: false },
    ])
    mockedApi.listConfig.mockImplementation((kind: string) =>
      Promise.resolve(kind === 'skills' ? [builtinRow] : []),
    )
    mockedApi.skillVersions.mockResolvedValue(versionRows)
    mockedApi.skillReferences.mockResolvedValue([])
  })

  const openBuiltinSkill = async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))
    fireEvent.click(screen.getByText('Skills'))
    await screen.findByText('email_triage_reply')
    fireEvent.click(screen.getByText('email_triage_reply'))
    await waitFor(() => expect(mockedApi.skillVersions).toHaveBeenCalledWith('email_triage_reply', undefined))
  }

  it('locks a platform built-in: read-only editor, no Save/Delete, copy-to-org instead', async () => {
    await openBuiltinSkill()

    const textarea = document.querySelector('textarea') as HTMLTextAreaElement
    expect(textarea).toHaveAttribute('readonly')
    expect(screen.queryByText('Save')).not.toBeInTheDocument()
    expect(screen.queryByText('Delete')).not.toBeInTheDocument()
    expect(screen.getByText(/Platform built-in/)).toBeInTheDocument()

    // Only active organisations are offered as copy targets.
    const copySelect = screen.getByLabelText('Copy to organisation')
    expect(copySelect).toBeInTheDocument()
    expect(within(copySelect).queryByRole('option', { name: 'Ghost' })).not.toBeInTheDocument()
    expect(within(copySelect).getByRole('option', { name: 'Acme' })).toBeInTheDocument()

    fireEvent.change(copySelect, { target: { value: 'acme' } })
    fireEvent.click(screen.getByText('Copy to organisation'))
    await waitFor(() =>
      expect(mockedApi.putConfigItem).toHaveBeenCalledWith(
        'skills',
        'email_triage_reply',
        expect.objectContaining({ instructions: 'triage' }),
        'acme',
      ),
    )
    await screen.findByText(/Copied to acme/)
  })

  it('shows a historical version read-only when picked from the version dropdown', async () => {
    await openBuiltinSkill()

    const versionSelect = screen.getByLabelText('Version')
    expect(versionSelect).toHaveValue('head')
    fireEvent.change(versionSelect, { target: { value: '1' } })

    const textarea = document.querySelector('textarea') as HTMLTextAreaElement
    expect(textarea.value).toBe(JSON.stringify({ instructions: 'triage v1' }, null, 2))
    expect(textarea).toHaveAttribute('readonly')
    expect(screen.getByText(/Historical version/)).toBeInTheDocument()
  })

  it('lists the deployed teams pinning the skill, and an empty state otherwise', async () => {
    mockedApi.skillReferences.mockResolvedValue([
      {
        org_name: 'acme', org_display_name: 'Acme', org_active: true,
        pipeline_name: 'maintenance_inbox', pipeline_version: 3,
        pinned_version: 1, is_current_deploy: true,
      },
      {
        org_name: 'acme', org_display_name: 'Acme', org_active: true,
        pipeline_name: 'maintenance_inbox', pipeline_version: 2,
        pinned_version: 1, is_current_deploy: false,
      },
    ])
    await openBuiltinSkill()

    await screen.findByText(/Acme .+ maintenance_inbox .+ pinned v1 .+ current deploy/)
    expect(screen.getByText(/superseded version/)).toBeInTheDocument()

    mockedApi.skillReferences.mockResolvedValue([])
    // Re-select to refetch: deselect by switching tab and back.
    fireEvent.click(screen.getByText('Pipelines'))
    fireEvent.click(screen.getByText('Skills'))
    await screen.findByText('email_triage_reply')
    fireEvent.click(screen.getByText('email_triage_reply'))
    await screen.findByText('No deployments reference this skill.')
  })
})

describe('AdvancedPage organisation selector', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listOrgs.mockResolvedValue([{ name: 'acme', display_name: 'Acme', active: true }])
    mockedApi.listConfig.mockResolvedValue([])
  })

  it('opens the Skills tab on the platform tier, and restores the organisation on an org-scoped tab', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))

    fireEvent.click(screen.getByText('Skills'))
    expect(screen.getByLabelText('Organisation')).toHaveValue('__platform__')
    await waitFor(() => expect(mockedApi.listConfig).toHaveBeenCalledWith('skills', undefined))

    fireEvent.click(screen.getByText('Pipelines'))
    expect(screen.getByLabelText('Organisation')).toHaveValue('acme')
  })

  it('offers "Show deactivated" even when every organisation is active', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Organisation')).toHaveValue('acme'))

    const toggle = screen.getByLabelText('Show deactivated')
    expect(toggle).not.toBeChecked()
  })
})
