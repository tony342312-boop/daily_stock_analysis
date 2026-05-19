import { describe, expect, test } from 'vitest';

import rawIndexJson from '../../../public/stocks.index.json?raw';
import type { StockIndexItem, StockIndexTuple } from '../../types/stockIndex';
import { INDEX_FIELD } from '../stockIndexFields';
import { searchStocks } from '../searchStocks';

const rawIndex = JSON.parse(rawIndexJson) as StockIndexTuple[];

const realIndex: StockIndexItem[] = rawIndex.map((tuple) => ({
  canonicalCode: tuple[INDEX_FIELD.CANONICAL_CODE],
  displayCode: tuple[INDEX_FIELD.DISPLAY_CODE],
  nameZh: tuple[INDEX_FIELD.NAME_ZH],
  pinyinFull: tuple[INDEX_FIELD.PINYIN_FULL],
  pinyinAbbr: tuple[INDEX_FIELD.PINYIN_ABBR],
  aliases: tuple[INDEX_FIELD.ALIASES],
  market: tuple[INDEX_FIELD.MARKET],
  assetType: tuple[INDEX_FIELD.ASSET_TYPE],
  active: tuple[INDEX_FIELD.ACTIVE],
  popularity: tuple[INDEX_FIELD.POPULARITY],
}));

describe('searchStocks real index constituent coverage', () => {
  test.each([
    // Nasdaq 100 constituents
    ['AAPL', 'AAPL'],
    ['Apple Inc', 'AAPL'],
    ['MSFT', 'MSFT'],
    ['Microsoft Corporation', 'MSFT'],
    ['NVDA', 'NVDA'],
    ['NVIDIA Corporation', 'NVDA'],
    ['AMZN', 'AMZN'],
    ['Amazon.com Inc', 'AMZN'],
    ['META', 'META'],
    ['Meta Platforms', 'META'],
    ['GOOGL', 'GOOGL'],
    ['Alphabet Inc', 'GOOGL'],
    ['AVGO', 'AVGO'],
    ['Broadcom Inc', 'AVGO'],
    ['TSLA', 'TSLA'],
    ['Tesla Inc', 'TSLA'],
    ['COST', 'COST'],
    ['Costco Wholesale', 'COST'],
    ['NFLX', 'NFLX'],
    ['Netflix', 'NFLX'],

    // S&P 500 constituents
    ['JPM', 'JPM'],
    ['JPMorgan Chase', 'JPM'],
    ['V', 'V'],
    ['Visa Inc', 'V'],
    ['UNH', 'UNH'],
    ['UnitedHealth Group', 'UNH'],
    ['XOM', 'XOM'],
    ['Exxon Mobil', 'XOM'],
    ['JNJ', 'JNJ'],
    ['Johnson & Johnson', 'JNJ'],
    ['PG', 'PG'],
    ['Procter & Gamble', 'PG'],
    ['HD', 'HD'],
    ['Home Depot', 'HD'],
    ['MA', 'MA'],
    ['Mastercard', 'MA'],
    ['WMT', 'WMT'],
    ['Walmart', 'WMT'],
    ['KO', 'KO'],
    ['Coca-Cola', 'KO'],

    // CSI 300 constituents
    ['600519', '600519.SH'],
    ['贵州茅台', '600519.SH'],
    ['300750', '300750.SZ'],
    ['宁德时代', '300750.SZ'],
    ['601318', '601318.SH'],
    ['中国平安', '601318.SH'],
    ['000858', '000858.SZ'],
    ['五粮液', '000858.SZ'],
    ['600036', '600036.SH'],
    ['招商银行', '600036.SH'],
    ['000333', '000333.SZ'],
    ['Midea Group', '000333.SZ'],
    ['600276', '600276.SH'],
    ['恒瑞医药', '600276.SH'],
    ['601166', '601166.SH'],
    ['兴业银行', '601166.SH'],
    ['002594', '002594.SZ'],
    ['比亚迪', '002594.SZ'],
    ['601398', '601398.SH'],
    ['工商银行', '601398.SH'],

    // Hang Seng Index constituents
    ['00700', '00700.HK'],
    ['腾讯控股', '00700.HK'],
    ['Tencent', '00700.HK'],
    ['09988', '09988.HK'],
    ['Alibaba', '09988.HK'],
    ['03690', '03690.HK'],
    ['Meituan', '03690.HK'],
    ['00005', '00005.HK'],
    ['HSBC Holdings', '00005.HK'],
    ['00941', '00941.HK'],
    ['China Mobile', '00941.HK'],
    ['01299', '01299.HK'],
    ['AIA', '01299.HK'],
    ['00883', '00883.HK'],
    ['CNOOC', '00883.HK'],
    ['02318', '02318.HK'],
    ['Ping An Insurance', '02318.HK'],
    ['03988', '03988.HK'],
    ['Bank of China', '03988.HK'],
    ['02020', '02020.HK'],
    ['ANTA Sports', '02020.HK'],

    // Hang Seng TECH Index constituents
    ['01810', '01810.HK'],
    ['Xiaomi', '01810.HK'],
    ['09618', '09618.HK'],
    ['JD.com', '09618.HK'],
    ['09888', '09888.HK'],
    ['Baidu', '09888.HK'],
    ['01024', '01024.HK'],
    ['Kuaishou', '01024.HK'],
    ['02015', '02015.HK'],
    ['Li Auto', '02015.HK'],
    ['09868', '09868.HK'],
    ['XPeng', '09868.HK'],
    ['09626', '09626.HK'],
    ['Bilibili', '09626.HK'],
    ['09961', '09961.HK'],
    ['Trip.com', '09961.HK'],
  ])('resolves constituent query %s to %s', (query, expectedCode) => {
    const results = searchStocks(query, realIndex, { limit: 8 });
    expect(results[0]?.canonicalCode).toBe(expectedCode);
  });
});
