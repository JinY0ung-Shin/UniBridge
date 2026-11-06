import { useState, type KeyboardEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  findPorts,
  getRegions,
  type NetworkProtocol,
  type PortFinderHost,
  type PortFinderQuery,
  type RequestedPortStatus,
} from '../../api/network';
import { useToast } from '../../components/useToast';
import { intOrUndefined, NETWORK_KEY, networkErrorMessage, portRange } from './networkUtils';
import { useAllNetworkHosts } from './useNetworkActions';
import PortStrip from './PortStrip';

/* "I am bringing up a service these clients must reach: which ports work today
 * without a firewall request?" Sources are hosts or whole regions; the
 * destination is one host, or every host ranked best first. */

const REQUESTED_BADGE: Record<RequestedPortStatus, string> = {
  free: 'badge-ok',
  used: 'badge-warn',
  blocked: 'badge-error',
  unreachable: 'badge-error',
};

const STRIPS_MAX = 6; // reachable spans drawn per host; the rest are summarised

interface Chip {
  kind: 'host' | 'region';
  value: string; // host name/IP, or region id
  label: string;
}

function RequestedBadge({ status, port }: { status: RequestedPortStatus | null; port: number | null }) {
  const { t } = useTranslation();
  if (status == null || port == null) return null;
  return (
    <span className={`badge ${REQUESTED_BADGE[status]}`}>
      {t(`network.finder.requested.${status}`, { port })}
    </span>
  );
}

function UsedList({ host }: { host: PortFinderHost }) {
  const { t } = useTranslation();
  if (host.used.length === 0) return null;
  return (
    <div className="finder-used">
      <span className="network-sub">{t('network.finder.usedPorts')}</span>
      <ul className="finder-chips">
        {host.used.map((u) => (
          <li key={u.port} className={`finder-chip finder-chip--used${u.kind === 'listening' ? ' finder-chip--listening' : ''}`}>
            <span className="mono">{u.port}</span>
            <span>{u.kind === 'listening' ? t('network.finder.listening') : u.service_name}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FinderHostCard({ host, port }: { host: PortFinderHost; port: number | null }) {
  const { t } = useTranslation();
  const { addToast } = useToast();
  const suggested = new Set(host.suggestions.map((s) => s.port));

  async function copy(p: number) {
    const text = `${host.host.ip}:${p}`;
    try {
      await navigator.clipboard.writeText(text);
      addToast({ type: 'success', title: t('network.finder.copied', { text }) });
    } catch {
      addToast({ type: 'error', title: t('network.check.copyFailed') });
    }
  }

  return (
    <article className="finder-card" aria-labelledby={`finder-host-${host.host.id}`}>
      <header className="finder-card__head">
        <div>
          <h3 id={`finder-host-${host.host.id}`} className="finder-card__title">
            <Link to={`/network/hosts/${host.host.id}`}>{host.host.name}</Link>
          </h3>
          <div className="network-sub">
            <span className="mono">{host.host.ip}</span> · {host.host.region_name ?? t('network.noRegion')}
            {host.host.owner && <> · {host.host.owner}</>}
          </div>
        </div>
        <div className="finder-card__stats">
          <RequestedBadge status={host.requested_port} port={port} />
          <span className="finder-card__count">
            {host.any_port
              ? t('network.finder.anyPort')
              : t('network.finder.freeCount', { count: host.free_count })}
          </span>
        </div>
      </header>

      {host.any_port ? (
        <p className="network-sub">{t('network.finder.anyPortHint')}</p>
      ) : (
        <>
          {host.suggestions.length > 0 && (
            <div className="finder-suggestions">
              <span className="network-sub">{t('network.finder.suggestions')}</span>
              <ul className="finder-chips">
                {host.suggestions.map((s) => (
                  <li key={s.port}>
                    <button
                      type="button"
                      className={`finder-chip finder-chip--${s.evidence === 'probed_free' ? 'probed' : 'recorded'}`}
                      title={s.evidence === 'probed_free'
                        ? t('network.finder.probedFreeHint', { sources: s.probed_from.join(', ') })
                        : t('network.finder.recordedHint')}
                      aria-label={t('network.finder.copyPort', { port: s.port })}
                      onClick={() => copy(s.port)}
                    >
                      <span className="mono">{s.port}</span>
                      {s.evidence === 'probed_free' && <span>{t('network.finder.probedFree')}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {host.reachable.length > 0 && (
            <div className="finder-strips">
              {host.reachable.slice(0, STRIPS_MAX).map((span) => (
                <PortStrip
                  key={`${span.port_from}-${span.port_to}`}
                  span={span}
                  used={host.used}
                  blocked={host.blocked}
                  suggested={suggested}
                />
              ))}
              {host.reachable.length > STRIPS_MAX && (
                <div className="network-sub">
                  {t('network.finder.moreSpans', {
                    spans: host.reachable.slice(STRIPS_MAX).map((s) => portRange(s.port_from, s.port_to)).join(', '),
                  })}
                </div>
              )}
            </div>
          )}
        </>
      )}

      <UsedList host={host} />
      {host.blocked.length > 0 && (
        <div className="network-note network-note--warn" role="note">
          {t('network.finder.blocked', {
            items: host.blocked.map((b) => `${b.port} (${b.src_host_name})`).join(', '),
          })}
        </div>
      )}
    </article>
  );
}

function PortFinderTab() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const hosts = useAllNetworkHosts().data ?? [];
  const regionsQuery = useQuery({ queryKey: [...NETWORK_KEY, 'regions'], queryFn: getRegions });
  const regions = regionsQuery.data ?? [];

  const [chips, setChips] = useState<Chip[]>(() => {
    const seen = new Set<string>();
    return searchParams.getAll('src').flatMap((raw) => {
      const s = raw.trim();
      if (!s || seen.has(s.toLowerCase())) return [];
      seen.add(s.toLowerCase());
      return [{ kind: 'host' as const, value: s, label: s }];
    });
  });
  const [draft, setDraft] = useState('');
  const [dst, setDst] = useState(searchParams.get('dst') ?? '');
  const [dstRegion, setDstRegion] = useState('');
  const [port, setPort] = useState('');
  const [protocol, setProtocol] = useState<NetworkProtocol>('tcp');
  const [lastPort, setLastPort] = useState<number | null>(null);
  // What the shown result was asked for, so editing the form does not relabel it.
  const [lastSingle, setLastSingle] = useState(false);
  const find = useMutation({ mutationFn: (q: PortFinderQuery) => findPorts(q) });
  const result = find.data;

  function addHost(value: string) {
    const v = value.trim();
    if (!v || chips.some((c) => c.kind === 'host' && c.value.toLowerCase() === v.toLowerCase())) {
      setDraft('');
      return;
    }
    setChips([...chips, { kind: 'host', value: v, label: v }]);
    setDraft('');
  }

  function addRegion(id: string) {
    const region = regions.find((r) => String(r.id) === id);
    if (!region || chips.some((c) => c.kind === 'region' && c.value === id)) return;
    setChips([...chips, { kind: 'region', value: id, label: t('network.finder.regionChip', { name: region.name }) }]);
  }

  function onDraftKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      addHost(draft);
    } else if (event.key === 'Backspace' && draft === '' && chips.length) {
      setChips(chips.slice(0, -1));
    }
  }

  function submit() {
    // A name still in the box counts, as if Enter had been pressed.
    const pending = draft.trim();
    const isNew = pending && !chips.some((c) => c.kind === 'host' && c.value.toLowerCase() === pending.toLowerCase());
    const all = isNew ? [...chips, { kind: 'host' as const, value: pending, label: pending }] : chips;
    if (draft.trim()) {
      setChips(all);
      setDraft('');
    }
    if (all.length === 0) return;
    const wanted = intOrUndefined(port);
    setLastPort(wanted ?? null);
    setLastSingle(!!dst.trim());
    find.mutate({
      src: all.filter((c) => c.kind === 'host').map((c) => c.value),
      srcRegions: all.filter((c) => c.kind === 'region').map((c) => Number(c.value)),
      dst: dst.trim() || undefined,
      dstRegion: dst.trim() ? undefined : intOrUndefined(dstRegion),
      port: wanted,
      protocol,
    });
  }

  const sameRegion = result?.hosts.filter((h) => h.any_port) ?? [];
  const viaOpenings = result?.hosts.filter((h) => !h.any_port) ?? [];
  const single = !!result && lastSingle;

  return (
    <div>
      <p className="page-subtitle">{t('network.finder.hint')}</p>
      <form
        className="finder-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <datalist id="finder-host-options">
          {hosts.flatMap((h) => [
            <option key={`n-${h.id}`} value={h.name}>{h.ip}</option>,
            <option key={`i-${h.id}`} value={h.ip}>{h.name}</option>,
          ])}
        </datalist>
        <div className="form-group finder-form__sources">
          <label htmlFor="finder-src">{t('network.finder.sources')}</label>
          <div className="finder-chip-input">
            <ul className="finder-chips" aria-label={t('network.finder.selectedSources')}>
              {chips.map((c, i) => (
                <li key={`${c.kind}-${c.value}`} className={`finder-chip finder-chip--${c.kind}`}>
                  <span>{c.label}</span>
                  <button
                    type="button"
                    className="finder-chip__remove"
                    aria-label={t('network.finder.removeSource', { name: c.label })}
                    onClick={() => setChips(chips.filter((_, j) => j !== i))}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
            <input
              id="finder-src"
              list="finder-host-options"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onDraftKey}
              onBlur={() => draft.trim() && addHost(draft)}
              placeholder={chips.length ? '' : t('network.finder.sourcesPlaceholder')}
            />
          </div>
          <div className="finder-form__region-add">
            <select
              aria-label={t('network.finder.addRegion')}
              value=""
              onChange={(e) => addRegion(e.target.value)}
            >
              <option value="">{t('network.finder.addRegion')}</option>
              {regions.map((r) => (
                <option key={r.id} value={r.id}>{t('network.finder.regionOption', { name: r.name, count: r.host_count })}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="form-group">
          <label htmlFor="finder-dst">{t('network.finder.destination')}</label>
          <input
            id="finder-dst"
            list="finder-host-options"
            value={dst}
            onChange={(e) => setDst(e.target.value)}
            placeholder={t('network.finder.destinationPlaceholder')}
          />
        </div>
        <div className="form-group">
          <label htmlFor="finder-dst-region">{t('network.finder.destinationRegion')}</label>
          <select
            id="finder-dst-region"
            value={dstRegion}
            disabled={!!dst.trim()}
            onChange={(e) => setDstRegion(e.target.value)}
          >
            <option value="">{t('network.allRegions')}</option>
            {regions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </div>
        <div className="form-group">
          <label htmlFor="finder-port">{t('network.finder.wantedPort')}</label>
          <input
            id="finder-port"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(e) => setPort(e.target.value)}
            placeholder={t('network.finder.wantedPortPlaceholder')}
          />
        </div>
        <div className="form-group">
          <label htmlFor="finder-protocol">{t('network.protocol')}</label>
          <select id="finder-protocol" value={protocol} onChange={(e) => setProtocol(e.target.value as NetworkProtocol)}>
            <option value="tcp">tcp</option>
            <option value="udp">udp</option>
          </select>
        </div>
        <div className="form-group network-check-form__submit">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={find.isPending || (chips.length === 0 && !draft.trim())}
            aria-busy={find.isPending}
          >
            {t('network.finder.run')}
          </button>
        </div>
      </form>

      {find.isError && (
        <div className="error-banner" role="alert">{networkErrorMessage(find.error, t('common.errorOccurred'))}</div>
      )}

      {result && (
        <section className="network-section" aria-labelledby="finder-result">
          <h2 id="finder-result" className="section-title">{t('network.finder.result')}</h2>
          <p className="network-sub">
            {t('network.finder.sourcesUsed', {
              count: result.sources.length,
              names: result.sources.map((s) => s.host_name ?? s.input).join(', '),
            })}
          </p>
          {result.unplaced_sources.length > 0 && (
            <div className="network-note network-note--warn" role="note">
              {t('network.finder.unplaced', { names: result.unplaced_sources.join(', ') })}
            </div>
          )}
          {result.truncated && <div className="network-note">{t('network.finder.truncated')}</div>}
          {result.unplaced_destinations.length > 0 && (
            <div className="network-note" role="note">
              {t('network.finder.unplacedDestinations', { names: result.unplaced_destinations.join(', ') })}
            </div>
          )}

          {result.hosts.length === 0 || (single && result.hosts[0].reachable_count === 0) ? (
            <div className="empty-state">
              <p>{single ? t('network.finder.noneForHost') : t('network.finder.none')}</p>
              <Link className="link-button" to="/network?tab=check">{t('network.finder.goCheck')}</Link>
            </div>
          ) : (
            <>
              {viaOpenings.length > 0 && (
                <>
                  {!single && <h3 className="network-subtitle">{t('network.finder.viaOpenings', { count: viaOpenings.length })}</h3>}
                  <div className="finder-cards">
                    {viaOpenings.map((h) => <FinderHostCard key={h.host.id} host={h} port={lastPort} />)}
                  </div>
                </>
              )}
              {sameRegion.length > 0 && (
                single ? (
                  <div className="finder-cards">
                    {sameRegion.map((h) => <FinderHostCard key={h.host.id} host={h} port={lastPort} />)}
                  </div>
                ) : (
                  <>
                    <h3 className="network-subtitle">{t('network.finder.sameRegion', { count: sameRegion.length })}</h3>
                    <p className="network-sub">{t('network.finder.anyPortHint')}</p>
                    <div className="table-container">
                      <table className="data-table" aria-label={t('network.finder.sameRegion', { count: sameRegion.length })}>
                        <thead>
                          <tr>
                            <th scope="col">{t('network.hosts.name')}</th>
                            <th scope="col">{t('network.region')}</th>
                            <th scope="col">{t('network.finder.usedPorts')}</th>
                            {lastPort != null && <th scope="col">{t('network.finder.wantedPort')}</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {sameRegion.map((h) => (
                            <tr key={h.host.id}>
                              <td>
                                <Link to={`/network/hosts/${h.host.id}`}>{h.host.name}</Link>
                                <div className="network-sub mono">{h.host.ip}</div>
                              </td>
                              <td>{h.host.region_name}</td>
                              <td className="mono">{h.used.map((u) => u.port).join(', ') || '—'}</td>
                              {lastPort != null && <td><RequestedBadge status={h.requested_port} port={lastPort} /></td>}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}

export default PortFinderTab;
