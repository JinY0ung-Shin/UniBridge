import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { createSavedQuery } from '../api/client';
import ResourceModal from './ResourceModal';
import { useToast } from './useToast';

interface SaveQueryModalProps {
  sql: string;
  databaseAlias: string | null;
  onClose: () => void;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const axiosErr = err as { response?: { data?: { detail?: string } } };
    const detail = axiosErr.response?.data?.detail;
    if (typeof detail === 'string') return detail;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return fallback;
}

function SaveQueryModal({ sql, databaseAlias, onClose }: SaveQueryModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () =>
      createSavedQuery({
        name: name.trim(),
        description: description.trim(),
        database_alias: databaseAlias,
        sql_text: sql,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['saved-queries'] });
      addToast({ type: 'success', title: t('queryPlayground.saveSuccess') });
      onClose();
    },
    onError: (err: unknown) => {
      setError(errorMessage(err, t('queryPlayground.saveFailed')));
    },
  });

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || createMutation.isPending) return;
    setError(null);
    createMutation.mutate();
  }

  return (
    <ResourceModal
      title={t('queryPlayground.saveModalTitle')}
      onClose={onClose}
      closeLabel={t('common.close')}
    >
      <form onSubmit={handleSubmit}>
        <div className="form-grid">
          <div className="form-group form-group--full">
            <label htmlFor="save-query-name">{t('common.name')}</label>
            <input
              id="save-query-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
              required
            />
          </div>
          <div className="form-group form-group--full">
            <label htmlFor="save-query-description">
              {t('queryPlayground.descriptionOptional')}
            </label>
            <input
              id="save-query-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={255}
            />
          </div>
        </div>

        {error && <div className="form-error">{error}</div>}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!name.trim() || createMutation.isPending}
          >
            {createMutation.isPending ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </form>
    </ResourceModal>
  );
}

export default SaveQueryModal;
