//! Coalesce proxy calls belonging to the same human turn. Pending text is
//! encrypted with the job. A claimed batch stays immutable until it succeeds.
use super::{
    Job, MAX_SOURCE, Settings, bounded_message_text, digest, put, queue_has_room, record,
    store_job, transaction,
};
use crate::conversation::{PartKind, Turn, human_block, tool_result};
use crate::traffic::Event;
use kaponir_vault::SqliteVault;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeSet, time::Duration};

pub(crate) struct Capture {
    messages: Value,
    pub answer: Answer,
}

impl Capture {
    pub(crate) fn into_input(mut self) -> Value {
        if self.messages.is_object() {
            self.messages["answer"] = Value::String(self.answer.text);
            self.messages["answer_truncated"] = Value::Bool(self.answer.truncated);
        }
        self.messages
    }
}

/// The learning source has its own size limit. The shorter diagnostic excerpt
/// displayed by the traffic inspector must not cut off the end of an answer.
#[derive(Default)]
pub(crate) struct Answer {
    text: String,
    block: Option<u64>,
    truncated: bool,
}

impl Answer {
    pub(crate) fn append(&mut self, slot: [u64; 2], wire: &str) {
        if slot[0] != 0 || wire.is_empty() || self.truncated {
            return;
        }
        let separator = self.block.is_some_and(|block| block != slot[1]);
        self.block = Some(slot[1]);
        if separator && self.text.len() < MAX_SOURCE {
            self.text.push('\n');
        }
        let remaining = MAX_SOURCE - self.text.len();
        self.truncated = crate::conversation::append_bounded(&mut self.text, wire, remaining);
    }
}

const IDLE_MS: u64 = 60_000;

fn human_message(message: &Value) -> bool {
    let content = &message["content"];
    message["role"] == "user"
        && (content.as_str().is_some_and(|s| !s.trim().is_empty())
            || content
                .as_array()
                .is_some_and(|a| a.iter().any(human_block)))
}

/// Capture the question and the tool results being delivered on this call.
/// Earlier tool exchanges already crossed the proxy. Never copy replayed chat
/// history, reasoning, tool arguments or binary attachments into the queue.
pub(crate) fn capture(messages: &Value) -> Capture {
    Capture {
        messages: capture_parts(messages),
        answer: Answer::default(),
    }
}

fn capture_parts(messages: &Value) -> Value {
    let Some(messages) = messages.as_array() else {
        return Value::Null;
    };
    let mut number = 0;
    let mut start = 0;
    for (i, _) in messages
        .iter()
        .enumerate()
        .filter(|(_, m)| human_message(m))
    {
        number += 1;
        start = i;
    }
    let tail = &messages[start..];
    let tools_start = tail
        .iter()
        .rposition(|m| m["role"] == "assistant")
        .map_or(0, |i| i + 1);
    let mut parts = Vec::new();
    let (mut user_bytes, mut tool_bytes) = (MAX_SOURCE, MAX_SOURCE);
    let mut partial = false;
    let mut add = |role: &str, content: &Value| {
        let budget = if role == "user" {
            &mut user_bytes
        } else {
            &mut tool_bytes
        };
        let (text, complete) = bounded_message_text(content, *budget);
        *budget -= text.len();
        partial |= !complete;
        if !text.trim().is_empty() {
            parts.push(json!({"role":role, "content":text}));
        }
    };
    for (index, m) in tail.iter().enumerate() {
        match m["role"].as_str() {
            Some("user") => {
                if let Some(blocks) = m["content"].as_array() {
                    for block in blocks {
                        if index >= tools_start && tool_result(block) {
                            add("tool", &block["content"]);
                        } else if index == 0
                            && matches!(block["type"].as_str(), None | Some("text"))
                        {
                            add("user", &block["text"]);
                        }
                    }
                } else if index == 0 {
                    add("user", &m["content"]);
                }
            }
            Some("tool" | "function") if index >= tools_start => add("tool", &m["content"]),
            _ => {}
        }
    }
    let mut input = json!({"number":number,"parts":parts});
    if partial {
        input["partial"] = Value::Bool(true);
    }
    input
}

#[derive(Clone, Serialize, Deserialize)]
struct Piece {
    event: u64,
    text: String,
}

#[derive(Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Release {
    Answer,
    Inactivity,
}

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct Question {
    session_id: String,
    turn_id: String,
    number: usize,
    events: BTreeSet<u64>,
    seen: BTreeSet<String>,
    pending: Vec<Piece>,
    // Byte boundaries in Job.source for the current batch, used by findings.
    origins: Vec<(usize, u64)>,
    pub(super) deadline: u64,
    release: Option<Release>,
    current_release: Option<Release>,
    completed_once: bool,
    processing_ms: u64,
    passes: usize,
    counted: usize,
    partial: bool,
}

impl Question {
    pub(super) fn summary(&self) -> Value {
        json!({"session_id":self.session_id,"turn_id":self.turn_id,"number":self.number,
            "event_id":self.events.first(),"requests":self.events.len(),"release":self.current_release.or(self.release),
            "passes":self.passes,"partial":self.partial})
    }

    fn add(&mut self, event: u64, text: &str) {
        let normalized = text.trim();
        if !normalized.is_empty() && self.seen.insert(digest(normalized)) {
            self.pending.push(Piece {
                event,
                text: text.into(),
            });
        } else if self.events.last().is_some_and(|latest| *latest > event)
            && let Some(part) = self
                .pending
                .iter_mut()
                .find(|p| p.event > event && p.text.trim() == normalized)
        {
            part.event = event;
        }
    }

    pub(super) fn clear_input(&mut self) {
        self.pending.clear();
        self.origins.clear();
    }

    pub(super) fn origin(&self, source: &str, value: &str) -> Option<u64> {
        let offset = source.find(value)?;
        self.origins
            .iter()
            .find(|(end, _)| *end > offset)
            .map(|(_, event)| *event)
    }
}

fn answer<'a>(event: &Event, input: &'a Value) -> Option<&'a str> {
    if event.usage.answer_finished == Some(false)
        || ((event.streaming || event.outcome != "ok") && event.usage.answer_finished != Some(true))
        || event.inspector.as_ref()?.reply.as_ref().is_some_and(|r| {
            r.parts
                .iter()
                .any(|p| p.slot[0] == 0 && p.kind == PartKind::Call)
        })
    {
        return None;
    }
    input["answer"].as_str().filter(|s| !s.trim().is_empty())
}

pub(super) fn collect(
    v: &SqliteVault,
    settings: &Settings,
    event: &Event,
    turn: &Turn,
    input: &Value,
    now: u64,
) -> anyhow::Result<()> {
    let Some(parts) = input.get("parts").unwrap_or(input).as_array() else {
        return Ok(());
    };
    let id = format!("learning:job:question:{}", turn.id);
    let old = record::<Job>(v, &id)?;
    if old
        .as_ref()
        .is_some_and(|j| matches!(j.state.as_str(), "cancelled" | "evicted" | "expired"))
    {
        return Ok(());
    }
    let previous_state = old.as_ref().map(|job| job.state.clone());
    let mut job = old.unwrap_or_else(|| {
        let session_id = event
            .session_id
            .clone()
            .unwrap_or_else(|| format!("request-{}", event.id));
        Job {
            name: session_id.clone(),
            id,
            kind: "turn".into(),
            state: "collecting".into(),
            created: now,
            updated: now,
            revision: settings.revision,
            model: settings.analysis.model.clone(),
            scenario: Some(settings.categorization.scenario),
            attempts: 0,
            extracted: 0,
            request_key: None,
            last_call: None,
            progress: None,
            started: None,
            source: String::new(),
            context: String::new(),
            error: String::new(),
            inference: Vec::new(),
            question: Some(Question {
                session_id,
                turn_id: turn.id.clone(),
                number: input["number"].as_u64().unwrap_or(0) as usize,
                events: BTreeSet::new(),
                seen: BTreeSet::new(),
                pending: Vec::new(),
                origins: Vec::new(),
                deadline: now + IDLE_MS,
                release: None,
                current_release: None,
                completed_once: false,
                processing_ms: 0,
                passes: 0,
                counted: 0,
                partial: false,
            }),
        }
    });
    let q = job.question.as_mut().expect("question job namespace");
    if !q.events.insert(event.id) {
        return Ok(());
    }
    for part in parts {
        if matches!(part["role"].as_str(), Some("user" | "tool" | "function"))
            && let Some(text) = part["content"].as_str()
        {
            q.add(event.id, text);
        }
    }
    let final_answer = answer(event, input);
    q.partial |=
        input["partial"] == true || final_answer.is_some() && input["answer_truncated"] == true;
    if let Some(text) = final_answer {
        q.add(event.id, text);
    }
    if q.events.last() == Some(&event.id) && (!q.pending.is_empty() || final_answer.is_some()) {
        q.deadline = now + IDLE_MS;
        q.release = final_answer.is_some().then_some(Release::Answer);
    }
    if previous_state.is_none() && q.pending.is_empty() {
        return Ok(());
    }
    if !q.pending.is_empty()
        && !matches!(job.state.as_str(), "running" | "failed")
        && job.source.is_empty()
    {
        let ready = q.release.is_some();
        job.requeue();
        job.state = if ready { "queued" } else { "collecting" }.into();
        job.revision = settings.revision;
        job.model.clone_from(&settings.analysis.model);
        job.scenario = Some(settings.categorization.scenario);
    }
    job.updated = now;
    if job.state != "succeeded" && !queue_has_room(v, &job, previous_state.as_deref())? {
        return Ok(());
    }
    // A later request can arrive while the model is running. Progress is owned
    // by the worker and stored separately, so preserve its latest snapshot.
    if job.state == "running" {
        put(v, &job.id, "learning-job-running", &job)?;
        let key = format!("meta:{}", job.id);
        let mut meta: Value = record(v, &key)?.unwrap_or_default();
        meta["question"] = job
            .question
            .as_ref()
            .map(Question::summary)
            .unwrap_or_default();
        meta["updated"] = json!(now);
        put(v, &key, "learning-job-meta", &meta)
    } else {
        store_job(v, &job)
    }
}

pub(super) fn release_due(
    v: &SqliteVault,
    now: u64,
    active: &BTreeSet<String>,
) -> anyhow::Result<()> {
    let due = v.due_questions(now, 64)?;
    if due.is_empty() {
        return Ok(());
    }
    transaction(v, || {
        for id in due {
            if id
                .strip_prefix("learning:job:question:")
                .is_some_and(|turn| active.contains(turn))
            {
                v.set_question_deadline(&id, Some(now + 1000))?;
                continue;
            }
            if let Some(mut job) = record::<Job>(v, &id)?
                && job.state == "collecting"
                && let Some(q) = &mut job.question
                && q.deadline <= now
            {
                q.release = Some(Release::Inactivity);
                job.requeue();
                store_job(v, &job)?;
            } else {
                v.set_question_deadline(&id, None)?;
            }
        }
        Ok(())
    })
}

pub(super) fn claim(job: &mut Job) {
    let Some(q) = &mut job.question else { return };
    if !job.source.is_empty() {
        return;
    } // retry or process restart
    q.current_release = q.release;
    q.pending.sort_by_key(|part| part.event);
    q.origins.clear();
    for part in &q.pending {
        if job.source.len() + usize::from(!job.source.is_empty()) + part.text.len() > MAX_SOURCE {
            break;
        }
        if !job.source.is_empty() {
            job.source.push('\n');
        }
        job.source.push_str(&part.text);
        q.origins.push((job.source.len(), part.event));
    }
    q.pending.drain(..q.origins.len());
}

/// Reload arrivals accepted during inference. A late continuation reuses this
/// job's URL and findings, and only unread material enters its next pass.
pub(super) fn finish(
    job: &mut Job,
    current: Option<Question>,
    succeeded: bool,
    elapsed: Duration,
) -> (Option<Duration>, usize) {
    job.question = current;
    let Some(q) = &mut job.question else {
        return (succeeded.then_some(elapsed), job.extracted);
    };
    if !succeeded {
        return (None, 0);
    }
    q.processing_ms += elapsed.as_millis() as u64;
    let completion_time =
        (q.pending.is_empty() && !q.completed_once).then(|| Duration::from_millis(q.processing_ms));
    q.completed_once |= completion_time.is_some();
    let batch_release = q.current_release.take();
    if q.pending.is_empty() {
        q.release = batch_release.or(q.release);
    }
    let extracted = job.extracted.saturating_sub(q.counted);
    q.counted = job.extracted;
    q.passes += 1;
    q.origins.clear();
    if !q.pending.is_empty() {
        if q.deadline <= job.updated && q.release.is_none() {
            q.release = Some(Release::Inactivity);
        }
        let collecting = q.release.is_none();
        job.requeue();
        if collecting {
            job.state = "collecting".into();
        }
    }
    (completion_time, extracted)
}

#[cfg(test)]
mod tests {
    use super::super::{Learning, list};
    use super::*;
    use crate::traffic::{EventInspector, Usage};

    fn vault() -> SqliteVault {
        SqliteVault::open_in_memory(&[47; 32]).unwrap()
    }

    fn step(
        v: &SqliteVault,
        call: u64,
        time: u64,
        messages: &Value,
        finished: bool,
        visible: &str,
    ) -> String {
        let turn = Turn::from_payload("session", &json!({"messages":messages})).unwrap();
        let id = format!("learning:job:question:{}", turn.id);
        let mut event:Event=serde_json::from_value(json!({"id":call,"time_ms":time,"provider":"openai","status":200,
            "outcome":"ok","duration_ms":1,"sent":true,"usage":Usage::default(),"session_id":"session"})).unwrap();
        event.streaming = true;
        event.inspector = Some(EventInspector::default());
        event.usage.record_finish(finished);
        let mut input = capture(messages);
        input.answer.append([0, 0], visible);
        collect(
            v,
            &Settings::default(),
            &event,
            &turn,
            &input.into_input(),
            time,
        )
        .unwrap();
        id
    }

    fn read(v: &SqliteVault, id: &str) -> Job {
        record(v, id).unwrap().unwrap()
    }

    fn start_job(v: &SqliteVault, id: &str) -> Job {
        let mut job = read(v, id);
        assert_eq!(job.state, "queued");
        claim(&mut job);
        job.state = "running".into();
        store_job(v, &job).unwrap();
        job
    }

    fn complete(v: &SqliteVault, job: &mut Job, time: u64) -> (bool, usize) {
        job.updated = time;
        job.source.clear();
        job.state = "succeeded".into();
        let (elapsed, extracted) = finish(
            job,
            read(v, &job.id).question,
            true,
            Duration::from_millis(10),
        );
        store_job(v, job).unwrap();
        (elapsed.is_some(), extracted)
    }

    #[test]
    fn question_tool_chain_has_one_batch_and_one_source_per_finding() {
        let v = vault();
        let user = json!({"role":"user","content":"read the project"});
        let call =
            json!({"role":"assistant","content":"internal commentary","tool_calls":[{"id":"a"}]});
        let tool = json!({"role":"tool","tool_call_id":"a","content":"CedarTool"});
        let id = step(&v, 1, 100, &json!([user]), false, "intermediate commentary");
        assert_eq!(read(&v, &id).state, "collecting");
        assert_eq!(v.app_record_count("learning-job-queued").unwrap(), 0);
        assert_eq!(id, step(&v, 2, 110, &json!([user, call, tool]), false, ""));
        let final_input = json!([user,call,tool,call,{"role":"tool","content":"second tool"}]);
        assert_eq!(
            id,
            step(&v, 3, 120, &final_input, true, "visible final answer")
        );
        step(&v, 3, 125, &final_input, true, "visible final answer");
        let mut job = start_job(&v, &id);
        assert_eq!(
            job.source,
            "read the project\nCedarTool\nsecond tool\nvisible final answer"
        );
        let q = job.question.as_ref().unwrap();
        assert_eq!(q.origin(&job.source, "CedarTool"), Some(2));
        assert_eq!(q.summary()["requests"], 3);
        let meta: Value = record(&v, &format!("meta:{id}")).unwrap().unwrap();
        assert!(!meta.to_string().contains("CedarTool"));
        assert_eq!(complete(&v, &mut job, 130), (true, 0));
        assert!(
            !String::from_utf8(v.app_record(&id).unwrap().unwrap())
                .unwrap()
                .contains("CedarTool")
        );
        assert_eq!(
            id,
            step(&v, 4, 140, &final_input, true, "visible final answer")
        );
        assert_eq!(read(&v, &id).state, "succeeded");
        let next = step(
            &v,
            5,
            150,
            &json!([user,call,tool,{"role":"user","content":"another question"}]),
            true,
            "another answer",
        );
        assert_ne!(id, next);
        assert_eq!(
            start_job(&v, &next).source,
            "another question\nanother answer"
        );
    }

    #[test]
    fn inactivity_restart_and_continuation_during_inference_keep_unseen_text() {
        let v = vault();
        let user = json!({"role":"user","content":"old input"});
        let id = step(&v, 10, 100, &json!([user]), false, "");
        let active = BTreeSet::from([read(&v, &id).question.unwrap().turn_id]);
        release_due(&v, 100 + IDLE_MS - 1, &BTreeSet::new()).unwrap();
        assert_eq!(read(&v, &id).state, "collecting");
        release_due(&v, 100 + IDLE_MS, &active).unwrap();
        assert_eq!(read(&v, &id).state, "collecting");
        // A restart retains both the pending input and the wall-clock deadline.
        Learning::load(&v).unwrap();
        release_due(&v, 1100 + IDLE_MS, &BTreeSet::new()).unwrap();
        let mut first = start_job(&v, &id);
        assert_eq!(first.source, "old input");
        assert_eq!(
            first.question.as_ref().unwrap().summary()["release"],
            "inactivity"
        );
        let history = json!([user,{"role":"assistant","content":null},{"role":"tool","content":"new result"}]);
        step(&v, 11, 1200 + IDLE_MS, &history, true, "final");
        assert_eq!(read(&v, &id).source, "old input");
        assert_eq!(complete(&v, &mut first, 1300 + IDLE_MS), (false, 0));
        let second = start_job(&v, &id);
        assert_eq!(second.source, "new result\nfinal");
        // A process interrupted after claiming replays the same immutable batch.
        Learning::load(&v).unwrap();
        let mut second = start_job(&v, &id);
        assert_eq!(second.source, "new result\nfinal");
        second.extracted = 2;
        assert_eq!(complete(&v, &mut second, 1500 + IDLE_MS), (true, 2));
        step(&v, 12, 1800 + IDLE_MS, &history, true, "later answer");
        assert_eq!(start_job(&v, &id).source, "later answer");
        assert_eq!(list::<Value>(&v, "learning-job-meta", 10).unwrap().len(), 1);
    }

    #[test]
    fn anthropic_blocks_do_not_turn_tool_results_reasoning_or_images_into_questions() {
        let user = json!({"role":"user","content":[{"type":"image","source":{"data":"binary"}},{"type":"text","text":"question"}]});
        let history = json!([{"role":"user","content":"older question"},{"role":"assistant","content":"old answer"},user,
            {"role":"assistant","content":[{"type":"thinking","thinking":"scratchpad"},{"type":"tool_use","id":"a","input":{"text":"arguments"}}]},
            {"role":"user","content":[{"type":"tool_result","tool_use_id":"a","content":[{"type":"text","text":"tool result"},{"type":"image","source":{"data":"binary"}}]}]}]);
        let input = capture_parts(&history);
        assert_eq!(
            input,
            json!({"number":2,"parts":[{"role":"user","content":"question"},{"role":"tool","content":"tool result"}]})
        );
        let v = vault();
        let id = step(&v, 7, 100, &history, true, "done");
        assert_eq!(start_job(&v, &id).source, "question\ntool result\ndone");
    }

    #[test]
    fn late_earlier_request_cannot_reopen_a_completed_answer_or_lose_its_tool() {
        let v = vault();
        let user = json!({"role":"user","content":"q"});
        let id = step(&v, 12, 100, &json!([user]), true, "answer");
        step(
            &v,
            11,
            120,
            &json!([user,{"role":"tool","content":"earlier output"}]),
            false,
            "",
        );
        assert_eq!(start_job(&v, &id).source, "q\nearlier output\nanswer");
    }

    #[test]
    fn switching_models_preserves_collecting_and_discard_forgets_pending_content() {
        use super::super::{QueueAction, queue_counts, switch_queue};
        let v = vault();
        let messages = json!([{"role":"user","content":"a pending question"}]);
        let id = step(&v, 1, 100, &messages, false, "");
        let engine = kaponir_core::Engine::builder([11; 32]).build().unwrap();
        let mut settings = Settings::default();
        settings.revision += 1;
        settings.analysis.provider = "api".into();
        settings.analysis.model = "next model".into();
        assert_eq!(queue_counts(&v).unwrap().0, 1);
        switch_queue(&v, &settings, QueueAction::Transfer, None, &engine).unwrap();
        let job = read(&v, &id);
        assert_eq!(
            (job.state.as_str(), job.model.as_str()),
            ("collecting", "next model")
        );
        assert_eq!(
            v.due_questions(100 + IDLE_MS, 10).unwrap(),
            std::slice::from_ref(&id)
        );
        settings.revision += 1;
        switch_queue(&v, &settings, QueueAction::Discard, None, &engine).unwrap();
        assert!(v.due_questions(100 + IDLE_MS, 10).unwrap().is_empty());
        assert!(
            !String::from_utf8(v.app_record(&id).unwrap().unwrap())
                .unwrap()
                .contains("a pending question")
        );
        step(&v, 2, 200, &messages, true, "late answer");
        assert_eq!(read(&v, &id).state, "cancelled");
        assert_eq!(queue_counts(&v).unwrap().0, 0);
    }

    #[test]
    fn large_tool_results_do_not_hide_the_question_or_the_final_reply() {
        let v = vault();
        let output = "a".repeat(MAX_SOURCE + 20);
        let messages =
            json!([{"role":"user","content":"read file"},{"role":"tool","content":output}]);
        let id = step(&v, 1, 100, &messages, true, "final answer");
        let mut first = start_job(&v, &id);
        assert_eq!(first.source, "read file");
        assert_eq!(first.question.as_ref().unwrap().summary()["partial"], true);
        assert_eq!(complete(&v, &mut first, 110), (false, 0));
        let mut second = start_job(&v, &id);
        assert_eq!(second.source.len(), MAX_SOURCE);
        assert_eq!(complete(&v, &mut second, 120), (false, 0));
        let mut last = start_job(&v, &id);
        assert_eq!(last.source, "final answer");
        assert_eq!(complete(&v, &mut last, 130), (true, 0));
    }

    #[test]
    fn visible_answer_uses_its_own_utf8_budget_and_only_the_first_choice() {
        let mut answer = Answer::default();
        answer.append([1, 0], "alternate");
        answer.append([0, 0], &"a".repeat(40_000));
        answer.append([0, 0], "after the inspector limit");
        answer.append([0, 1], "second block");
        assert!(
            answer
                .text
                .ends_with("after the inspector limit\nsecond block")
        );
        assert!(!answer.text.contains("alternate"));
        answer.append([0, 1], &"я".repeat(MAX_SOURCE / 2));
        assert!(answer.truncated && (MAX_SOURCE - 1..=MAX_SOURCE).contains(&answer.text.len()));
    }
}
