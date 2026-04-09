import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getGatewayUpstreams,
  saveGatewayUpstream,
  deleteGatewayUpstream,
  type GatewayUpstream,
} from '../api/client';
import './GatewayUpstreams.css';

interface NodeEntry {
  host: string;
  port: string;
  weight: string;
}

const emptyNode: NodeEntry = { host: '', port: '80', weight: '1' };

function nodesToEntries(nodes: Record<string, number>): NodeEntry[] {
  return Object.entries(nodes).map(([addr, weight]) => {
    const [host, port] = addr.split(':');
    return { host, port: port || '80', weight: String(weight) };
  });
}

function entriesToNodes(entries: NodeEntry[]): Record<string, number> {
  const nodes: Record<string, number> = {};
  for (const e of entries) {
    if (e.host.trim()) {
      nodes[`${e.host.trim()}:${e.port || '80'}`] = Number(e.weight) || 1;
    }
  }
  return nodes;
}

function GatewayUpstreams() {
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [type, setType] = useState('roundrobin');
  const [nodes, setNodes] = useState<NodeEntry[]>([{ ...emptyNode }]);
  const [error, setError] = useState('');

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  const saveMutation = useMutation({
    mutationFn: (data: { id: string; body: Record<string, unknown> }) =>
      saveGatewayUpstream(data.id, data.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-upstreams'] });
      closeModal();
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Failed to save upstream');
      } else {
        setError('Failed to save upstream');
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGatewayUpstream(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-upstreams'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? 'Failed to delete upstream');
      }
    },
  });

  const upstreams = upstreamsQuery.data?.items ?? [];

  function openCreate() {
    setEditingId(null);
    setName('');
    setType('roundrobin');
    setNodes([{ ...emptyNode }]);
    setError('');
    setShowModal(true);
  }

  function openEdit(u: GatewayUpstream) {
    setEditingId(u.id);
    setName(u.name || '');
    setType(u.type || 'roundrobin');
    setNodes(nodesToEntries(u.nodes || {}).length > 0 ? nodesToEntries(u.nodes) : [{ ...emptyNode }]);
    setError('');
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingId(null);
    setError('');
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const upstreamId = editingId || Date.now().toString();
    const body = {
      name: name.trim() || undefined,
      type,
      nodes: entriesToNodes(nodes),
    };
    setError('');
    saveMutation.mutate({ id: upstreamId, body });
  }

  function handleDelete(u: GatewayUpstream) {
    const label = u.name || u.id;
    if (window.confirm(`Delete upstream "${label}"?`)) {
      deleteMutation.mutate(u.id);
    }
  }

  function updateNode(index: number, field: keyof NodeEntry, value: string) {
    setNodes((prev) => prev.map((n, i) => (i === index ? { ...n, [field]: value } : n)));
  }

  function addNode() {
    setNodes((prev) => [...prev, { ...emptyNode }]);
  }

  function removeNode(index: number) {
    setNodes((prev) => prev.filter((_, i) => i !== index));
  }

  function formatNodes(nodesObj: Record<string, number>): string {
    return Object.entries(nodesObj)
      .map(([addr, w]) => `${addr} (w:${w})`)
      .join(', ');
  }

  return (
    <div className="gateway-upstreams">
      <div className="page-header">
        <div>
          <h1>Gateway Upstreams</h1>
          <p className="page-subtitle">Manage backend server groups</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Upstream</button>
      </div>

      {upstreamsQuery.isLoading && <div className="loading-message">Loading upstreams...</div>}
      {upstreamsQuery.isError && <div className="error-banner">Failed to load upstreams.</div>}

      {upstreams.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Nodes</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {upstreams.map((u) => (
                <tr key={u.id}>
                  <td className="cell-alias">{u.name || u.id}</td>
                  <td><span className="badge badge-type">{u.type}</span></td>
                  <td className="cell-nodes">{formatNodes(u.nodes || {})}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(u)}>Edit</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(u)} disabled={deleteMutation.isPending}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!upstreamsQuery.isLoading && upstreams.length === 0 && !upstreamsQuery.isError && (
        <div className="empty-state">
          <h3>No upstreams</h3>
          <p>Click "Add Upstream" to register a backend server group.</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingId ? 'Edit Upstream' : 'Add Upstream'}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group">
                  <label>Name</label>
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-backend" />
                  <span className="field-hint">식별을 위한 이름 (선택사항, 예: user-api, payment-server)</span>
                </div>
                <div className="form-group">
                  <label>Type</label>
                  <select value={type} onChange={(e) => setType(e.target.value)}>
                    <option value="roundrobin">Round Robin</option>
                    <option value="chash">Consistent Hash</option>
                    <option value="ewma">EWMA</option>
                    <option value="least_conn">Least Connections</option>
                  </select>
                  <span className="field-hint">트래픽을 노드에 분배하는 로드밸런싱 알고리즘</span>
                </div>
                <div className="form-group form-group--full">
                  <label>Nodes</label>
                  <span className="field-hint">요청을 처리할 백엔드 서버 목록. Weight가 높을수록 더 많은 트래픽을 받습니다</span>
                  <div className="nodes-list">
                    <div className="node-row node-row--header">
                      <span className="node-label node-host">Host / IP</span>
                      <span className="node-label node-port">Port</span>
                      <span className="node-label node-weight">Weight</span>
                    </div>
                    {nodes.map((node, idx) => (
                      <div key={idx} className="node-row">
                        <input
                          className="node-host"
                          placeholder="e.g. 192.168.1.10 or api.example.com"
                          value={node.host}
                          onChange={(e) => updateNode(idx, 'host', e.target.value)}
                          required
                        />
                        <input
                          className="node-port"
                          placeholder="8080"
                          type="number"
                          value={node.port}
                          onChange={(e) => updateNode(idx, 'port', e.target.value)}
                        />
                        <input
                          className="node-weight"
                          placeholder="1"
                          type="number"
                          value={node.weight}
                          onChange={(e) => updateNode(idx, 'weight', e.target.value)}
                        />
                        {nodes.length > 1 && (
                          <button type="button" className="node-remove" onClick={() => removeNode(idx)}>&times;</button>
                        )}
                      </div>
                    ))}
                    <button type="button" className="btn btn-sm btn-secondary add-node-btn" onClick={addNode}>
                      + Add Node
                    </button>
                  </div>
                </div>
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
                  {saveMutation.isPending ? 'Saving...' : editingId ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default GatewayUpstreams;
