import { createContext, useContext } from "react";

export type Venue = "toss" | "upbit";
export const MarketContext = createContext<Venue>("toss");
export const marketInfo = (venue: Venue) =>
  venue === "upbit"
    ? {
        name: "업비트 코인",
        currency: "KRW",
        unit: "개",
        example: "KRW-BTC",
        budget: "1000000",
        maxBudget: 10000000,
      }
    : {
        name: "토스 미국 주식",
        currency: "USD",
        unit: "주",
        example: "AAPL",
        budget: "1000",
        maxBudget: 5000,
      };
export const formatMoney = (value: unknown, venue: Venue = "toss") =>
  value == null
    ? "—"
    : new Intl.NumberFormat("ko-KR", {
        style: "currency",
        currency: marketInfo(venue).currency,
        maximumFractionDigits: venue === "upbit" ? 8 : 2,
      }).format(Number(value));
export function useMarket() {
  const venue = useContext(MarketContext);
  return {
    venue,
    ...marketInfo(venue),
    money: (value: unknown) => formatMoney(value, venue),
  };
}
