import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    smart_reply_sla: {
      executor: "ramping-arrival-rate",
      startRate: 2,
      timeUnit: "1s",
      preAllocatedVUs: 20,
      maxVUs: 120,
      stages: [
        { target: 10, duration: "1m" },
        { target: 20, duration: "2m" },
        { target: 40, duration: "2m" },
        { target: 0, duration: "30s" },
      ],
      tags: { flow: "smart_reply" },
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<15000", "p(99)<20000"],
    checks: ["rate>0.99"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8023";
const PLATFORM = __ENV.PLATFORM || "douyin";

const payloads = [
  {
    message: "仙女山两日游现在多少钱？",
    customer_name: "压测用户_A",
    customer_id: "load_a",
    platform: PLATFORM,
    conversation_history: [
      { role: "user", content: "我想看武隆的线路" },
      { role: "assistant", content: "好的，您更关注价格还是行程安排？" },
    ],
  },
  {
    message: "刚才那个套餐包含门票和住宿吗？",
    customer_name: "压测用户_B",
    customer_id: "load_b",
    platform: PLATFORM,
    conversation_history: [
      { role: "user", content: "请介绍一下两日游" },
      { role: "assistant", content: "两日游包含景区和住宿，可继续帮您确认细节。" },
    ],
  },
  {
    message: "如果我要投诉怎么处理？",
    customer_name: "压测用户_C",
    customer_id: "load_c",
    platform: PLATFORM,
    conversation_history: [
      { role: "user", content: "我对服务有意见" },
      { role: "assistant", content: "抱歉给您带来不便，我先协助记录问题。" },
    ],
  },
  {
    message: "这个产品能优惠一点吗？",
    customer_name: "压测用户_D",
    customer_id: "load_d",
    platform: PLATFORM,
    conversation_history: [
      { role: "user", content: "预算有限，有没有折扣？" },
      { role: "assistant", content: "我可以先帮您看一下当前活动。" },
    ],
  },
];

export default function () {
  const payload = payloads[Math.floor(Math.random() * payloads.length)];
  const response = http.post(`${BASE_URL}/api/smart-reply`, JSON.stringify(payload), {
    headers: {
      "Content-Type": "application/json",
    },
    timeout: "20s",
    tags: { endpoint: "smart_reply" },
  });

  check(response, {
    "status is 200": (res) => res.status === 200,
    "body is json": (res) => {
      try {
        JSON.parse(res.body);
        return true;
      } catch (error) {
        return false;
      }
    },
    "success is true": (res) => {
      try {
        return JSON.parse(res.body).success === true;
      } catch (error) {
        return false;
      }
    },
    "reply or need_human exists": (res) => {
      try {
        const body = JSON.parse(res.body);
        return Boolean(body.reply) || body.need_human === true;
      } catch (error) {
        return false;
      }
    },
  });

  sleep(0.2);
}
