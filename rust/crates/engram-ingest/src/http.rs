//! Blocking HTTP transport for ingestion pollers.
//!
//! This module is the I/O adapter. The pollers in `issue_poll` own the
//! pagination and deletion policy; keeping the transport separate makes the
//! unit-tier coverage gate measure business logic instead of reqwest plumbing.

use std::collections::BTreeMap;
use std::time::Duration;

use reqwest::blocking::Client as HttpClient;
use serde_json::Value;

use crate::issue_poll::PollError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    /// Header names are lower-cased.
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .get(&name.to_ascii_lowercase())
            .map(String::as_str)
    }
}

pub trait HttpTransport: Send + Sync {
    fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError>;
    fn post_json(
        &self,
        url: &str,
        headers: &[(&str, String)],
        body: &Value,
    ) -> Result<HttpResponse, PollError>;
}

/// Blocking reqwest transport. Same-origin redirects are followed; a token is
/// never forwarded to another origin.
pub struct ReqwestTransport {
    client: HttpClient,
}

impl ReqwestTransport {
    pub fn new(timeout: Duration) -> Result<Self, PollError> {
        let client = HttpClient::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::custom(|attempt| {
                let Some(original) = attempt.previous().first() else {
                    return attempt.follow();
                };
                if urls_same_origin(original, attempt.url()) {
                    attempt.follow()
                } else {
                    attempt.error("cross-origin redirect blocked")
                }
            }))
            .build()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        Ok(Self { client })
    }
}

impl HttpTransport for ReqwestTransport {
    fn get(&self, url: &str, headers: &[(&str, String)]) -> Result<HttpResponse, PollError> {
        let mut request = self.client.get(url);
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let response = request
            .send()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        response_parts(response)
    }

    fn post_json(
        &self,
        url: &str,
        headers: &[(&str, String)],
        body: &Value,
    ) -> Result<HttpResponse, PollError> {
        let mut request = self.client.post(url).json(body);
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let response = request
            .send()
            .map_err(|error| PollError::Transport(error.to_string()))?;
        response_parts(response)
    }
}

fn response_parts(response: reqwest::blocking::Response) -> Result<HttpResponse, PollError> {
    let status = response.status().as_u16();
    let mut headers = BTreeMap::new();
    for (name, value) in response.headers() {
        if let Ok(value) = value.to_str() {
            headers
                .entry(name.as_str().to_ascii_lowercase())
                .and_modify(|existing: &mut String| {
                    existing.push_str(", ");
                    existing.push_str(value);
                })
                .or_insert_with(|| value.to_string());
        }
    }
    let body = response
        .bytes()
        .map_err(|error| PollError::Transport(error.to_string()))?
        .to_vec();
    Ok(HttpResponse {
        status,
        headers,
        body,
    })
}

pub(crate) fn urls_same_origin(candidate: &reqwest::Url, base: &reqwest::Url) -> bool {
    candidate.scheme() == base.scheme()
        && candidate.host_str() == base.host_str()
        && candidate.port_or_known_default() == base.port_or_known_default()
}
