import { Suspense, lazy } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider } from './components/AuthProvider';
import { ThemeProvider } from './components/ThemeContext';
import { ToastProvider } from './components/ToastContext';
import App from './App';
import { isUploadRelayPath } from './utils/uploadRelay';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
});

// Loaded on demand: the app's own bundle does not carry the relay page.
const UploadRelay = lazy(() => import('./pages/UploadRelay'));

/**
 * What the page at this address is: the app, or the upload relay popup.
 *
 * The relay popup (pages/UploadRelay.tsx) is not part of the app: a small
 * window another app opens, with no navigation and none of the app's data
 * loading, so it stays out of the app Layout and AuthProvider and signs in on
 * its own. The rule is the same as the app's, though: an account without an
 * app role gets the page's own pending-approval state, not the file picker.
 */
export default function Root() {
  if (isUploadRelayPath(window.location.pathname)) {
    return (
      <ThemeProvider>
        <Suspense fallback={null}>
          <UploadRelay />
        </Suspense>
      </ThemeProvider>
    );
  }
  return (
    <BrowserRouter>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <ToastProvider>
              <App />
            </ToastProvider>
          </AuthProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}
