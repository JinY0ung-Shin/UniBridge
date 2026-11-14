import { describe, it, expect } from 'vitest';
import { apiErrorDetail, apiErrorMessage, detailText, errorMessage } from '../utils/apiError';

function httpError(detail: unknown, status = 400) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    response: { status, data: detail === undefined ? {} : { detail } },
  });
}

describe('detailText', () => {
  it('passes a sentence through', () => {
    expect(detailText('Alias already exists')).toBe('Alias already exists');
  });

  it('joins the messages of a validation list', () => {
    expect(detailText([
      { loc: ['body', 'port'], msg: 'Input should be a valid integer', type: 'int_parsing' },
      { loc: ['body', 'host'], msg: 'Field required', type: 'missing' },
    ])).toBe('Input should be a valid integer; Field required');
  });

  it('reads an object detail, with its id when it has one', () => {
    expect(detailText({ message: 'This opening is already recorded', id: 7 }))
      .toBe('This opening is already recorded (#7)');
    expect(detailText({ message: 'Conflict' })).toBe('Conflict');
  });

  it('gives nothing for a blank, empty or unknown detail', () => {
    expect(detailText('')).toBeUndefined();
    expect(detailText('   ')).toBeUndefined();
    expect(detailText([])).toBeUndefined();
    expect(detailText([{ loc: ['body'] }])).toBeUndefined();
    expect(detailText({ loc: ['paths'] })).toBeUndefined();
    expect(detailText(null)).toBeUndefined();
    expect(detailText(42)).toBeUndefined();
  });
});

describe('apiErrorDetail / apiErrorMessage', () => {
  it('reads every detail shape off an HTTP error', () => {
    expect(apiErrorDetail(httpError('Forbidden', 403))).toBe('Forbidden');
    expect(apiErrorDetail(httpError([{ loc: ['body', 'uri'], msg: 'bad uri' }], 422))).toBe('bad uri');
    expect(apiErrorDetail(httpError({ message: 'dup', id: 3 }, 409))).toBe('dup (#3)');
  });

  it('gives nothing when the server sent no detail or the error has no response', () => {
    expect(apiErrorDetail(httpError(undefined, 502))).toBeUndefined();
    expect(apiErrorDetail({ response: { data: '<html>502</html>' } })).toBeUndefined();
    expect(apiErrorDetail(new Error('Network Error'))).toBeUndefined();
    expect(apiErrorDetail(undefined)).toBeUndefined();
    expect(apiErrorDetail('boom')).toBeUndefined();
  });

  it('falls back when there is no detail', () => {
    expect(apiErrorMessage(httpError('Nope'), 'Save failed')).toBe('Nope');
    expect(apiErrorMessage(httpError(undefined, 500), 'Save failed')).toBe('Save failed');
    expect(apiErrorMessage(new Error('Network Error'), 'Save failed')).toBe('Save failed');
  });
});

describe('errorMessage', () => {
  it('prefers the server detail, else the fallback, for an HTTP error', () => {
    expect(errorMessage(httpError([{ msg: 'too long' }], 422), 'Failed')).toBe('too long');
    // the axios sentence "Request failed with status code 500" is never shown
    expect(errorMessage(httpError(undefined, 500), 'Failed')).toBe('Failed');
  });

  it('shows the message of an error that never got a response', () => {
    expect(errorMessage(new Error('Invalid JSON in sample params'), 'Failed')).toBe('Invalid JSON in sample params');
    expect(errorMessage(new Error(''), 'Failed')).toBe('Failed');
    expect(errorMessage('boom', 'Failed')).toBe('Failed');
  });
});
