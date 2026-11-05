import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  deleteFirewallOpening,
  deleteNetworkHost,
  deletePortUsage,
  getNetworkHosts,
  getRegions,
  verifyFirewallOpening,
  verifyNetworkHost,
  verifyPortUsage,
} from '../../api/network';
import { useToast } from '../../components/useToast';
import { NETWORK_KEY, networkErrorMessage } from './networkUtils';

/** A network write: refreshes every network query, toasts the backend's reason on failure. */
export function useNetworkMutation<TVars, TData>(
  mutationFn: (vars: TVars) => Promise<TData>,
  errorTitleKey: string,
  onSuccess?: (data: TData, vars: TVars) => void,
) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  return useMutation({
    mutationFn,
    onSuccess: (data, vars) => {
      queryClient.invalidateQueries({ queryKey: NETWORK_KEY });
      onSuccess?.(data, vars);
    },
    onError: (err) => addToast({
      type: 'error',
      title: t(errorTitleKey),
      message: networkErrorMessage(err) || undefined,
    }),
  });
}

/** Row actions shared by the list tabs and the host detail page. */
export function useNetworkRowActions() {
  return {
    verifyOpening: useNetworkMutation(verifyFirewallOpening, 'network.verifyFailed'),
    deleteOpening: useNetworkMutation(deleteFirewallOpening, 'network.deleteFailed'),
    verifyUsage: useNetworkMutation(verifyPortUsage, 'network.verifyFailed'),
    deleteUsage: useNetworkMutation(deletePortUsage, 'network.deleteFailed'),
    verifyHost: useNetworkMutation(verifyNetworkHost, 'network.verifyFailed'),
    deleteHost: useNetworkMutation(deleteNetworkHost, 'network.deleteFailed'),
  };
}

/** Every host, for the host select boxes and the access check suggestions. */
export function useAllNetworkHosts() {
  return useQuery({
    queryKey: [...NETWORK_KEY, 'hosts', {}],
    queryFn: () => getNetworkHosts(),
  });
}

export function useRegions() {
  return useQuery({ queryKey: [...NETWORK_KEY, 'regions'], queryFn: getRegions });
}
