import { createContext, useContext } from "react";

export type Venue = "toss" | "upbit" | "upbit_usdt";
export const isCrypto = (venue: Venue) => venue !== "toss";
export const MarketContext = createContext<Venue>("toss");
export const marketInfo = (venue: Venue) =>
  ({
    toss: {
      name: "토스 미국 주식",
      currency: "USD",
      unit: "주",
      example: "AAPL",
      budget: "1000",
      maxBudget: 5000,
    },
    upbit: {
      name: "업비트 원화",
      currency: "KRW",
      unit: "개",
      example: "KRW-BTC",
      budget: "1000000",
      maxBudget: 10000000,
    },
    upbit_usdt: {
      name: "업비트 USDT",
      currency: "USDT",
      unit: "개",
      example: "USDT-SOL",
      budget: "500",
      maxBudget: 10000,
    },
  })[venue];
export const formatMoney = (value: unknown, venue: Venue = "toss") => {
  if (value == null) return "—";
  if (venue === "upbit_usdt")
    return (
      new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 8 }).format(
        Number(value),
      ) + " USDT"
    );
  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency: marketInfo(venue).currency,
    maximumFractionDigits: isCrypto(venue) ? 8 : 2,
  }).format(Number(value));
};
export function useMarket() {
  const venue = useContext(MarketContext);
  return {
    venue,
    ...marketInfo(venue),
    money: (value: unknown) => formatMoney(value, venue),
  };
}
