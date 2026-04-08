import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getGatewayConsumers,
  saveGatewayConsumer,
  deleteGatewayConsumer,
  type GatewayConsumer,
} from '../api/client';
import './GatewayConsumers.css';

function generateKey(): string {
  return 'key-' + crypto.randomUUID().replace(/-/g, '');
}

function GatewayConsumers() {
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingUsername, setEditingUsername] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const consumersQuery = useQuery({
    queryKey: ['gateway-consumers'],
    queryFn: getGatewayConsumers,
  });

  const saveMutation = useMutation({
    mutationFn: (data: { username: string; body: Record<string, unknown> }) =>
      saveGatewayConsumer(data.username, data.body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['gateway-consumers'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Failed to save consumer');
      } else {
        setError('Failed to save consumer');
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (uname: string) => deleteGatewayConsumer(uname),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-consumers'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? 'Failed to delete consumer');
      }
    },
  });

  const consumers = consumersQuery.data?.items ?? [];

  function openCreate() {
    setEditingUsername(null);
    setUsername('');
    setApiKey(generateKey());
    setError('');
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function openEdit(c: GatewayConsumer) {
    setEditingUsername(c.username);
    setUsername(c.username);
    setApiKey('');
    setError('');
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingUsername(null);
    setCreatedKey(null);
    setError('');
    setCopied(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim()) return;
    const body: Record<string, unknown> = {};
    if (apiKey.trim()) {
      body.api_key = apiKey.trim();
    }
    setError('');
    saveMutation.mutate({ username: username.trim(), body });
  }

  function handleDelete(c: GatewayConsumer) {
    if (window.confirm(`Delete consumer "${c.username}"?`)) {
      deleteMutation.mutate(c.username);
    }
  }

  async function handleCopy() {
    if (createdKey) {
      await navigator.clipboard.writeText(createdKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <div className="gateway-consumers">
      <div className="page-header">
        <div>
          <h1>Gateway Consumers</h1>
          <p className="page-subtitle">Manage API consumers and their authentication keys</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Consumer</button>
      </div>

      {consumersQuery.isLoading && <div className="loading-message">Loading consumers...</div>}
      {consumersQuery.isError && <div className="error-banner">Failed to load consumers.</div>}

      {consumers.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>API Key</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {consumers.map((c) => (
                <tr key={c.username}>
                  <td className="cell-alias">{c.username}</td>
                  <td className="cell-key">{c.api_key || '—'}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(c)}>Edit</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(c)} disabled={deleteMutation.isPending}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!consumersQuery.isLoading && consumers.length === 0 && !consumersQuery.isError && (
        <div className="empty-state">
          <h3>No consumers</h3>
          <p>Click "Add Consumer" to create an API consumer with a key.</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={createdKey ? undefined : closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingUsername ? 'Edit Consumer' : 'Add Consumer'}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>

            {createdKey ? (
              <>
                <div className="key-created-banner">
                  <p>API key created. Copy it now — you won't be able to see it again.</p>
                  <div className="key-display">
                    <code>{createdKey}</code>
                    <button className="copy-btn" onClick={handleCopy}>
                      {copied ? 'Copied!' : 'Copy'}
                    </button>
                  </div>
                </div>
                <div className="modal-actions">
                  <button className="btn btn-primary" onClick={closeModal}>Done</button>
                </div>
              </>
            ) : (
              <form onSubmit={handleSubmit}>
                <div className="form-grid">
                  <div className="form-group form-group--full">
                    <label>Username</label>
                    <input
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      placeholder="my-app"
                      required
                      disabled={!!editingUsername}
                    />
                  </div>
                  <div className="form-group form-group--full">
                    <label>API Key</label>
                    <input
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={editingUsername ? 'Leave empty to keep current' : 'Auto-generated key'}
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary generate-btn"
                      onClick={() => setApiKey(generateKey())}
                    >
                      Generate New Key
                    </button>
                  </div>
                </div>

                {error && <div className="form-error">{error}</div>}

                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                  <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
                    {saveMutation.isPending ? 'Saving...' : editingUsername ? 'Update' : 'Create'}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default GatewayConsumers;
