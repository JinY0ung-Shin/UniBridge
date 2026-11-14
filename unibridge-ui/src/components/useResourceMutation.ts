import { useMutation, useQueryClient, type QueryKey } from '@tanstack/react-query';
import { useToast } from './useToast';
import { apiErrorDetail } from '../utils/apiError';

type ErrorMode =
  | { kind: 'toast'; title: string }
  | { kind: 'setError'; setError: (msg: string) => void; fallback: string };

interface ResourceMutationOptions<TVars, TData> {
  mutationFn: (vars: TVars) => Promise<TData>;
  invalidateKey?: QueryKey;
  onSuccess?: (data: TData, vars: TVars) => void;
  errorMode?: ErrorMode;
}

export function useResourceMutation<TVars, TData>(opts: ResourceMutationOptions<TVars, TData>) {
  const qc = useQueryClient();
  const { addToast } = useToast();
  return useMutation({
    mutationFn: opts.mutationFn,
    onSuccess: (data, vars) => {
      if (opts.invalidateKey) {
        qc.invalidateQueries({ queryKey: opts.invalidateKey });
      }
      opts.onSuccess?.(data, vars);
    },
    onError: (err) => {
      const detail = apiErrorDetail(err);
      const mode = opts.errorMode;
      if (!mode) return;
      if (mode.kind === 'toast') {
        addToast({ type: 'error', title: mode.title, message: detail });
      } else {
        mode.setError(detail ?? mode.fallback);
      }
    },
  });
}
