using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;

namespace TpcJpPatch.Tests
{
    sealed class AssertException : Exception
    {
        public AssertException(string message) : base(message) { }
    }

    sealed class SkipException : Exception
    {
        public SkipException(string message) : base(message) { }
    }

    /// <summary>Assertions; a failed one throws AssertException with a readable message.</summary>
    static class T
    {
        public static void True(bool condition, string what)
        {
            if (!condition)
                throw new AssertException(what);
        }

        public static void Eq<TV>(TV expected, TV actual, string what)
        {
            if (!EqualityComparer<TV>.Default.Equals(expected, actual))
                throw new AssertException($"{what}: expected <{Show(expected)}>, got <{Show(actual)}>");
        }

        public static void Bytes(byte[] expected, byte[] actual, string what)
        {
            if (expected.Length != actual.Length)
                throw new AssertException($"{what}: length {expected.Length} expected, got {actual.Length}");
            for (int i = 0; i < expected.Length; i++)
                if (expected[i] != actual[i])
                    throw new AssertException($"{what}: first difference at byte {i}");
        }

        /// <summary>The action must throw exactly TEx (or a subclass); any other exception type is a failure.</summary>
        public static TEx Throws<TEx>(Action action, string what) where TEx : Exception
        {
            try
            {
                action();
            }
            catch (TEx e)
            {
                return e;
            }
            catch (Exception e)
            {
                throw new AssertException($"{what}: expected {typeof(TEx).Name}, got {e.GetType().FullName}: {e.Message}");
            }
            throw new AssertException($"{what}: expected {typeof(TEx).Name}, nothing was thrown");
        }

        public static void Contains(string haystack, string needle, string what)
        {
            if (haystack == null || haystack.IndexOf(needle, StringComparison.Ordinal) < 0)
                throw new AssertException($"{what}: <{Show(haystack)}> does not contain <{needle}>");
        }

        public static string Show(object o)
        {
            if (o == null)
                return "null";
            string s = o.ToString();
            s = s.Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
            return s.Length > 160 ? s.Substring(0, 160) + "…" : s;
        }
    }

    /// <summary>Runs named tests; each prints PASS / FAIL / XFAIL / XPASS / SKIP.</summary>
    sealed class Runner
    {
        public int Passed, Failed, XFailed, XPassed, Skipped;
        public readonly List<string> Failures = new List<string>();
        public readonly List<string> ExpectedFailures = new List<string>();
        public readonly List<string> UnexpectedPasses = new List<string>();
        readonly string only;
        readonly TextWriter log;

        public Runner(string only, TextWriter log)
        {
            this.only = only;
            this.log = log;
        }

        void Print(string line)
        {
            Console.WriteLine(line);
            log?.WriteLine(line);
            log?.Flush();
        }

        public void Note(string line) => Print("     " + line);

        /// <param name="xfail">Finding id when the test documents a known product problem: a failure is
        /// reported as XFAIL (not counted), a pass as XPASS (the finding looks fixed).</param>
        public void Test(string name, Action body, string xfail = null)
        {
            if (only != null && only.Split(',').All(o => name.IndexOf(o, StringComparison.OrdinalIgnoreCase) < 0))
                return;
            var sw = Stopwatch.StartNew();
            string error = null;
            try
            {
                body();
            }
            catch (SkipException e)
            {
                Skipped++;
                Print($"SKIP  {name} ({e.Message})");
                return;
            }
            catch (AssertException e)
            {
                error = e.Message;
            }
            catch (Exception e)
            {
                error = "unexpected " + e.GetType().FullName + ": " + e.Message + Environment.NewLine + e.StackTrace;
            }
            string time = $"[{sw.ElapsedMilliseconds} ms]";
            if (xfail != null)
            {
                if (error != null)
                {
                    XFailed++;
                    ExpectedFailures.Add($"{name} [{xfail}]: {error}");
                    Print($"XFAIL {name} [{xfail}] {time}");
                    Print("      " + error.Replace(Environment.NewLine, Environment.NewLine + "      "));
                }
                else
                {
                    XPassed++;
                    UnexpectedPasses.Add($"{name} [{xfail}]");
                    Print($"XPASS {name} [{xfail}] {time} (the finding appears fixed; remove the xfail mark)");
                }
                return;
            }
            if (error == null)
            {
                Passed++;
                Print($"PASS  {name} {time}");
            }
            else
            {
                Failed++;
                Failures.Add($"{name}: {error}");
                Print($"FAIL  {name} {time}");
                Print("      " + error.Replace(Environment.NewLine, Environment.NewLine + "      "));
            }
        }
    }
}
